"""
Teste offline do motor do Pump Watch (sinal de acumulação, independente do desafio de
portfólio principal — ver screener/pump_watch.py). Não faz chamadas de rede reais: monkeypatch
de sources_coingecko.fetch_market_chart/fetch_by_ids para simular histórico e reavaliações.

Corre com: python -m tests.test_pump_watch_offline
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import config, pump_watch, sources_coingecko as cg  # noqa: E402


def fake_candidate(symbol, price_usd, turnover=0.2, cid=None):
    return {
        "tier": "cex_small_cap",
        "id": cid or symbol.lower(),
        "symbol": symbol,
        "price_usd": price_usd,
        "turnover": turnover,
    }


def fake_chart(prices, volumes):
    """prices/volumes: listas simples de valores (não pares) — converte para o formato
    [timestamp_ms, valor] que o CoinGecko devolve de facto."""
    return {
        "prices": [[i * 3_600_000, p] for i, p in enumerate(prices)],
        "volumes": [[i * 3_600_000, v] for i, v in enumerate(volumes)],
    }


def _accumulating_chart(hours=72, base_price=1.0):
    """Preço quase plano no fim (2 subidas pequenas para cada descida), mas volume MUITO maior
    nas subidas do que nas descidas — OBV líquido fortemente positivo (compra a acumular-se)
    com o preço ainda por reagir, exatamente o padrão que o sinal deve apanhar."""
    price = base_price
    prices, volumes = [price], [1000]
    for i in range(1, hours):
        if i % 3 != 0:
            price *= 1.003   # 2 em cada 3 passos: subida pequena, volume alto
            vol = 1500 + i * 10
        else:
            price *= 0.994   # 1 em cada 3 passos: descida maior, volume baixo (pouca venda)
            vol = 300
        prices.append(price)
        volumes.append(vol)
    return fake_chart(prices, volumes)


def _flat_no_volume_chart(hours=72, base_price=1.0):
    """Preço e volume planos — sem sinal de acumulação nem de distribuição."""
    return fake_chart([base_price] * hours, [10] * hours)


def _already_pumped_chart(hours=72, base_price=1.0):
    """Já subiu >15% na janela — fora da banda de 'acumulação silenciosa' (já é momentum)."""
    prices = [base_price * (1 + 0.30 * i / hours) for i in range(hours)]
    volumes = [1000 + i * 80 for i in range(hours)]
    return fake_chart(prices, volumes)


def run():
    with tempfile.TemporaryDirectory() as tmp:
        config.PUMP_WATCH_STATE_FILE = os.path.join(tmp, "pump_watch_state.json")
        eur_rate = 0.9  # taxa fixa para o teste ser determinístico

        _scenario_signal_math()
        _scenario_entry_and_shortlist(eur_rate)
        _scenario_entry_venue(eur_rate)
        _scenario_trailing_stop_from_entry(eur_rate)
        _scenario_trailing_stop_after_rise(eur_rate)
        _scenario_profit_reserve_split(eur_rate)
        _scenario_loss_no_reserve_skim(eur_rate)
        _scenario_no_scan_when_slots_full(eur_rate)
        _scenario_state_corrupted()

        print("\n✅ Todos os testes offline do Pump Watch passaram.")


def _scenario_signal_math():
    accumulating = pump_watch._compute_accumulation_signal(**_accumulating_chart())
    assert accumulating is not None
    assert accumulating["obv_score"] >= config.PUMP_WATCH_MIN_OBV_SCORE, (
        f"FALHOU: gráfico com volume crescente nas subidas devia dar obv_score alto, "
        f"deu {accumulating['obv_score']:.3f}"
    )

    flat = pump_watch._compute_accumulation_signal(**_flat_no_volume_chart())
    assert flat is not None
    assert flat["obv_score"] == 0, "FALHOU: preço perfeitamente plano devia dar obv_score 0"

    pumped = pump_watch._compute_accumulation_signal(**_already_pumped_chart())
    assert pumped["price_change_pct"] > config.PUMP_WATCH_MAX_PRICE_MOVE_PCT, (
        "FALHOU: cenário 'já subiu 30%' devia exceder a banda de acumulação silenciosa"
    )

    too_short = pump_watch._compute_accumulation_signal(
        [[0, 1.0], [3_600_000, 1.01]], [[0, 100], [3_600_000, 110]]
    )
    assert too_short is None, "FALHOU: histórico curto demais (<24 pontos) devia devolver None"

    print("✅ Sinal OK — OBV distingue acumulação, plano e já-em-momentum corretamente")


def _scenario_entry_and_shortlist(eur_rate):
    """10 candidatos, mas só 2 slots livres e um deles já em carteira — confirma que o
    shortlist respeita PUMP_WATCH_SHORTLIST_SIZE e que só os melhores por obv_score entram."""
    state = pump_watch._default_state()

    accumulating_chart = _accumulating_chart()
    flat_chart = _flat_no_volume_chart()

    original_fetch_chart = cg.fetch_market_chart
    original_fetch_by_ids = cg.fetch_by_ids
    try:
        # ALPHA e BETA acumulam (elegíveis), os restantes ficam planos (não elegíveis)
        def fake_fetch_market_chart(coin_id, days):
            if coin_id in ("alpha", "beta"):
                return accumulating_chart
            return flat_chart

        cg.fetch_market_chart = fake_fetch_market_chart

        candidates = [fake_candidate("ALPHA", 1.0), fake_candidate("BETA", 2.0)]
        candidates += [fake_candidate(f"NOISE{i}", 1.0) for i in range(8)]  # 10 no total

        actions = pump_watch._check_entries(state, candidates, eur_rate, now=1000.0)

        assert len(actions) == 2, f"FALHOU: esperava 2 entradas (ALPHA+BETA), teve {len(actions)}"
        symbols = {a["symbol"] for a in actions}
        assert symbols == {"ALPHA", "BETA"}, f"FALHOU: esperava ALPHA+BETA, teve {symbols}"
        assert state["status"] == "active"
        assert state["start_ts"] == 1000.0
        assert len(state["positions"]) == 2

        equity_after = state["cash_eur"] + sum(
            p["qty"] * p["last_price_eur"] for p in state["positions"].values()
        )
        assert abs(equity_after - config.PUMP_WATCH_STARTING_BALANCE_EUR) < 0.01, (
            "FALHOU: equity logo após compras devia igualar o saldo inicial"
        )
    finally:
        cg.fetch_market_chart = original_fetch_chart
        cg.fetch_by_ids = original_fetch_by_ids

    print(f"✅ Entradas OK — {len(state['positions'])} posições abertas por sinal de acumulação "
          f"(candidatos sem sinal corretamente ignorados)")


def _scenario_entry_venue(eur_rate):
    """Nota da venue mais líquida na compra (pedido do Ricardo 2026-09-14, caso SOXSB — o
    preço guardado é uma média do CoinGecko entre várias venues, mas uma compra real só pode
    ser executada numa de cada vez; ver sources_coingecko.fetch_top_venue). Pump Watch é
    sempre cex_small_cap (ver docstring do módulo), por isso a nota deve aparecer em toda
    entrada real."""
    state = pump_watch._default_state()
    accumulating_chart = _accumulating_chart()

    original_fetch_chart = cg.fetch_market_chart
    original_fetch_top_venue = cg.fetch_top_venue
    try:
        cg.fetch_market_chart = lambda coin_id, days: accumulating_chart
        cg.fetch_top_venue = lambda coin_id: "KCEX"

        candidates = [fake_candidate("SOXSB", 50.59, cid="soxsb-token")]
        actions = pump_watch._check_entries(state, candidates, eur_rate, now=1000.0)

        assert len(actions) == 1
        assert actions[0]["entry_venue"] == "KCEX", (
            f"FALHOU: compra do Pump Watch devia guardar a venue mais líquida: {actions[0]}"
        )
        key = list(state["positions"].keys())[0]
        assert state["positions"][key]["entry_venue"] == "KCEX"

        # uma falha na chamada de venue (rate limit, rede, etc.) nunca deve bloquear a compra
        state2 = pump_watch._default_state()

        def _boom(coin_id):
            raise RuntimeError("simulated API failure")

        cg.fetch_top_venue = _boom
        actions2 = pump_watch._check_entries(state2, candidates, eur_rate, now=2000.0)
        assert len(actions2) == 1
        assert actions2[0]["entry_venue"] is None, (
            "FALHOU: falha na chamada de venue não devia impedir a entrada do Pump Watch"
        )
    finally:
        cg.fetch_market_chart = original_fetch_chart
        cg.fetch_top_venue = original_fetch_top_venue

    print("✅ Venue OK — nota da venue mais líquida guardada nas entradas do Pump Watch "
          "(e a falha na chamada não bloqueia a compra)")


def _scenario_trailing_stop_from_entry(eur_rate):
    """Posição que cai logo após a entrada, sem nunca subir — o 'pico' arranca no preço de
    entrada, por isso isto deve comportar-se como um stop-loss simples de -10%."""
    state = pump_watch._default_state()
    state["positions"]["cex_small_cap:gamma"] = {
        "id": "gamma", "symbol": "GAMMA", "qty": 100.0,
        "entry_price_eur": 1.0, "entry_ts": 0, "cost_eur": 100.0,
        "last_price_eur": 1.0, "peak_price_eur": 1.0,
    }

    original_fetch_by_ids = cg.fetch_by_ids
    try:
        # cai -9% — ainda não deve fechar
        cg.fetch_by_ids = lambda ids: {"gamma": {"price_usd": (1.0 * 0.91) / eur_rate}}
        actions = pump_watch._check_exits(state, eur_rate, now=100.0)
        assert not actions, "FALHOU: -9% desde a entrada não devia disparar o trailing de -10%"
        assert "cex_small_cap:gamma" in state["positions"]

        # cai -11% — agora deve fechar
        cg.fetch_by_ids = lambda ids: {"gamma": {"price_usd": (1.0 * 0.89) / eur_rate}}
        actions = pump_watch._check_exits(state, eur_rate, now=200.0)
        assert len(actions) == 1, "FALHOU: -11% desde a entrada devia disparar o trailing de -10%"
        assert "cex_small_cap:gamma" not in state["positions"]
        assert actions[0]["pnl_pct"] < 0
    finally:
        cg.fetch_by_ids = original_fetch_by_ids

    print("✅ Trailing desde a entrada OK — -9% não fecha, -11% fecha (peak = preço de entrada)")


def _scenario_trailing_stop_after_rise(eur_rate):
    """Posição que sobe bastante e depois recua 10% do pico (não do preço de entrada) —
    confirma que o pico é seguido corretamente ao longo de várias corridas."""
    state = pump_watch._default_state()
    state["positions"]["cex_small_cap:delta"] = {
        "id": "delta", "symbol": "DELTA", "qty": 100.0,
        "entry_price_eur": 1.0, "entry_ts": 0, "cost_eur": 100.0,
        "last_price_eur": 1.0, "peak_price_eur": 1.0,
    }

    original_fetch_by_ids = cg.fetch_by_ids
    try:
        # sobe para 2.0 (pico) — sem fechar
        cg.fetch_by_ids = lambda ids: {"delta": {"price_usd": 2.0 / eur_rate}}
        actions = pump_watch._check_exits(state, eur_rate, now=100.0)
        assert not actions
        assert state["positions"]["cex_small_cap:delta"]["peak_price_eur"] == 2.0

        # recua para 1.85 (-7.5% do pico de 2.0) — ainda não fecha
        cg.fetch_by_ids = lambda ids: {"delta": {"price_usd": 1.85 / eur_rate}}
        actions = pump_watch._check_exits(state, eur_rate, now=200.0)
        assert not actions, "FALHOU: -7.5% do pico não devia disparar o trailing de -10%"
        # pico mantém-se em 2.0, não desce com o preço
        assert state["positions"]["cex_small_cap:delta"]["peak_price_eur"] == 2.0

        # recua para 1.79 (-10.5% do pico de 2.0) — fecha, com lucro (entrada foi 1.0)
        cg.fetch_by_ids = lambda ids: {"delta": {"price_usd": 1.79 / eur_rate}}
        actions = pump_watch._check_exits(state, eur_rate, now=300.0)
        assert len(actions) == 1, "FALHOU: -10.5% do pico devia disparar o trailing"
        assert actions[0]["pnl_eur"] > 0, "FALHOU: devia fechar com lucro (saiu acima da entrada)"
    finally:
        cg.fetch_by_ids = original_fetch_by_ids

    print("✅ Trailing após subida OK — pico persiste corretamente, só fecha com recuo real de 10%")


def _scenario_profit_reserve_split(eur_rate):
    """Fecho com lucro: 20% do LUCRO (não do valor total) vai para a reserva, o resto + o
    capital investido volta ao saldo negociável."""
    state = pump_watch._default_state()
    state["cash_eur"] = 0.0  # isola o efeito do fecho
    state["positions"]["cex_small_cap:epsilon"] = {
        "id": "epsilon", "symbol": "EPSILON", "qty": 100.0,
        "entry_price_eur": 1.0, "entry_ts": 0, "cost_eur": 100.0,
        "last_price_eur": 1.0, "peak_price_eur": 1.0,
    }

    trade = pump_watch._close_position(state, "cex_small_cap:epsilon", exit_price_eur=1.5, reason="teste", now=0)

    assert abs(trade["pnl_eur"] - 50.0) < 0.001, f"FALHOU: pnl esperado 50.0, teve {trade['pnl_eur']}"
    assert abs(trade["reserved_eur"] - 10.0) < 0.001, (
        f"FALHOU: reserva esperada 20% de 50 = 10.0, teve {trade['reserved_eur']}"
    )
    assert abs(state["reserve_eur"] - 10.0) < 0.001
    # cash recebe: proceeds (150) - reservado (10) = 140 (= 100 capital + 40 dos 80% do lucro)
    assert abs(state["cash_eur"] - 140.0) < 0.001, f"FALHOU: cash esperado 140.0, teve {state['cash_eur']}"

    print("✅ Reserva de lucro OK — 20% do LUCRO (10.0 de 50.0) reservado, resto + capital ao saldo negociável")


def _scenario_loss_no_reserve_skim(eur_rate):
    """Fecho com perda: nenhum corte para a reserva — o valor todo da venda volta ao saldo."""
    state = pump_watch._default_state()
    state["cash_eur"] = 0.0
    state["positions"]["cex_small_cap:zeta"] = {
        "id": "zeta", "symbol": "ZETA", "qty": 100.0,
        "entry_price_eur": 1.0, "entry_ts": 0, "cost_eur": 100.0,
        "last_price_eur": 1.0, "peak_price_eur": 1.0,
    }

    trade = pump_watch._close_position(state, "cex_small_cap:zeta", exit_price_eur=0.9, reason="teste", now=0)

    assert trade["pnl_eur"] < 0
    assert trade["reserved_eur"] == 0.0, "FALHOU: uma perda não deve reservar nada"
    assert state["reserve_eur"] == 0.0
    assert abs(state["cash_eur"] - 90.0) < 0.001, f"FALHOU: cash esperado 90.0 (proceeds cheio), teve {state['cash_eur']}"

    print("✅ Perda sem corte OK — nenhuma reserva criada, valor todo da venda volta ao saldo")


def _scenario_no_scan_when_slots_full(eur_rate):
    """Com as 3 posições já ocupadas, _check_entries não deve sequer tentar buscar histórico
    (fetch_market_chart nunca chamado) — confirma o 'só verifica quando há slot livre'."""
    state = pump_watch._default_state()
    for i in range(config.PUMP_WATCH_MAX_POSITIONS):
        state["positions"][f"cex_small_cap:full{i}"] = {
            "id": f"full{i}", "symbol": f"FULL{i}", "qty": 1.0,
            "entry_price_eur": 1.0, "entry_ts": 0, "cost_eur": 10.0,
            "last_price_eur": 1.0, "peak_price_eur": 1.0,
        }

    calls = []
    original_fetch_chart = cg.fetch_market_chart
    try:
        cg.fetch_market_chart = lambda coin_id, days: calls.append(coin_id) or _accumulating_chart()
        candidates = [fake_candidate("NEWCOIN", 1.0)]
        actions = pump_watch._check_entries(state, candidates, eur_rate, now=0)
        assert actions == [], "FALHOU: sem slots livres não devia haver novas entradas"
        assert calls == [], "FALHOU: sem slots livres não devia nem chamar fetch_market_chart (poupa API)"
    finally:
        cg.fetch_market_chart = original_fetch_chart

    print("✅ Slots cheios OK — nenhuma chamada de histórico desperdiçada quando não há vaga")


def _scenario_state_corrupted():
    import tempfile as tf
    with tf.TemporaryDirectory() as tmp2:
        bad_path = os.path.join(tmp2, "pump_watch_state.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{ isto não é json válido")
        original = config.PUMP_WATCH_STATE_FILE
        config.PUMP_WATCH_STATE_FILE = bad_path
        try:
            raised = False
            try:
                pump_watch.load_state()
            except pump_watch.PumpWatchStateCorrupted:
                raised = True
            assert raised, "FALHOU: JSON inválido devia levantar PumpWatchStateCorrupted"
        finally:
            config.PUMP_WATCH_STATE_FILE = original

    print("✅ Estado corrompido OK — levanta PumpWatchStateCorrupted em vez de reiniciar em silêncio")


if __name__ == "__main__":
    run()
