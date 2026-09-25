"""
Teste offline do motor do Pump Watch (sinal de acumulação, independente do desafio de
portfólio principal — ver screener/pump_watch.py). Não faz chamadas de rede reais: monkeypatch
de sources_coingecko.fetch_market_chart/fetch_by_ids para simular histórico e reavaliações.

Corre com: python -m tests.test_pump_watch_offline
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import config, pump_watch, sources_coingecko as cg, telegram_alert  # noqa: E402


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
        awakening = 3.0 if i >= hours - 12 else 1.0  # últimas 12h: volume a acordar (~3x)
        if i % 3 != 0:
            price *= 1.003   # 2 em cada 3 passos: subida pequena, volume alto
            vol = (1500 + i * 10) * awakening
        else:
            price *= 0.994   # 1 em cada 3 passos: descida maior, volume baixo (pouca venda)
            vol = 300 * awakening
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
        config.CATALYST_ENABLED = False  # sem rede nos testes; ligado só nos cenários de catalisadores (com mocks)
        eur_rate = 0.9  # taxa fixa para o teste ser determinístico

        _scenario_signal_math()
        _scenario_entry_and_shortlist(eur_rate)
        _scenario_entry_venue(eur_rate)
        _scenario_entry_contract_address(eur_rate)
        _scenario_trailing_stop_from_entry(eur_rate)
        _scenario_trailing_stop_after_rise(eur_rate)
        _scenario_profit_reserve_split(eur_rate)
        _scenario_loss_no_reserve_skim(eur_rate)
        _scenario_no_scan_when_slots_full(eur_rate)
        _scenario_state_corrupted()
        _scenario_review_cycles()
        _scenario_lsk_like_volume_awakening()
        _scenario_calm_filter_shortlist(eur_rate)
        _scenario_news_parsing()
        _scenario_catalysts(eur_rate)

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


def _scenario_entry_contract_address(eur_rate):
    """Endereço do contrato na listagem de posições abertas (pedido do Ricardo 2026-09-14) —
    Pump Watch é sempre cex_small_cap (ver docstring do módulo), por isso a nota vem sempre de
    sources_coingecko.fetch_contract_address, chamada só no momento da compra."""
    state = pump_watch._default_state()
    accumulating_chart = _accumulating_chart()

    original_fetch_chart = cg.fetch_market_chart
    original_fetch_top_venue = cg.fetch_top_venue
    original_fetch_contract_address = cg.fetch_contract_address
    try:
        cg.fetch_market_chart = lambda coin_id, days: accumulating_chart
        cg.fetch_top_venue = lambda coin_id: "KCEX"
        cg.fetch_contract_address = lambda coin_id: "0x1234567890000000000000000000000000dEaD (BSC)"

        candidates = [fake_candidate("SOXSB", 50.59, cid="soxsb-token")]
        actions = pump_watch._check_entries(state, candidates, eur_rate, now=1000.0)

        assert len(actions) == 1
        assert actions[0]["entry_contract_address"] == "0x1234567890000000000000000000000000dEaD (BSC)", (
            f"FALHOU: compra do Pump Watch devia guardar o endereço do contrato: {actions[0]}"
        )
        key = list(state["positions"].keys())[0]
        assert state["positions"][key]["entry_contract_address"] == "0x1234567890000000000000000000000000dEaD (BSC)"

        msg = telegram_alert.format_pump_watch_message(state, actions, eur_rate)
        position_line = next(ln for ln in msg.split("\n") if ln.strip().startswith("•") and "SOXSB" in ln)
        assert "0x1234567890000000000000000000000000dEaD (BSC)" in position_line, (
            f"FALHOU: listagem de posições abertas do Pump Watch devia mostrar o contrato: {position_line}"
        )

        # uma falha na chamada de endereço (rate limit, rede, etc.) nunca deve bloquear a compra
        state2 = pump_watch._default_state()

        def _boom(coin_id):
            raise RuntimeError("simulated API failure")

        cg.fetch_contract_address = _boom
        actions2 = pump_watch._check_entries(state2, candidates, eur_rate, now=2000.0)
        assert len(actions2) == 1
        assert actions2[0]["entry_contract_address"] is None, (
            "FALHOU: falha na chamada de endereço não devia impedir a entrada do Pump Watch"
        )
        msg2 = telegram_alert.format_pump_watch_message(state2, actions2, eur_rate)
        position_line2 = next(ln for ln in msg2.split("\n") if ln.strip().startswith("•") and "SOXSB" in ln)
        assert "📝" not in position_line2, (
            f"FALHOU: sem endereço, a linha da posição não devia mostrar a nota: {position_line2}"
        )
    finally:
        cg.fetch_market_chart = original_fetch_chart
        cg.fetch_top_venue = original_fetch_top_venue
        cg.fetch_contract_address = original_fetch_contract_address

    print("✅ Endereço do contrato OK — guardado nas entradas do Pump Watch e mostrado na "
          "listagem de posições abertas (e a falha na chamada não bloqueia a compra)")


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


def _scenario_review_cycles():
    """Pedido do Ricardo 2026-09-24: o marco de revisão (10 trades OU 20 dias) repete-se em
    ciclos contínuos, sem reiniciar saldo nem posições."""
    from screener import telegram_alert
    now = time.time()
    st = pump_watch._default_state()
    st.update({"status": "active", "start_ts": now - 5 * 86400, "cash_eur": 50.0, "reserve_eur": 2.0})
    st["positions"]["cex_small_cap:hold"] = {"id": "hold", "symbol": "HOLD", "qty": 50.0,
                                             "entry_price_eur": 1.0, "last_price_eur": 1.0}
    # ciclo #1 ainda aberto: 5 dias e 1 trade
    st["closed_trades"] = [{"symbol": "A", "pnl_eur": 3.0, "pnl_pct": 10.0, "exit_reason": "x", "exit_ts": now}]
    assert pump_watch._maybe_close_review_cycle(st, now) is None, "FALHOU: ciclo #1 ainda não devia fechar"

    # 10 trades fechados -> fecha o ciclo #1 por número de trades
    st["closed_trades"] += [{"symbol": f"L{i}", "pnl_eur": -1.0, "pnl_pct": -5.0, "exit_reason": "x",
                             "exit_ts": now} for i in range(9)]
    r1 = pump_watch._maybe_close_review_cycle(st, now)
    assert r1 and r1["number"] == 1 and r1["trigger"] == "trades" and r1["num_trades"] == 10
    assert abs(r1["win_rate_pct"] - 10.0) < 1e-9 and abs(r1["end_total_eur"] - 102.0) < 1e-9
    assert st["review_cycle"]["number"] == 2 and st["review_cycle"]["start_trade_index"] == 10
    assert st["cash_eur"] == 50.0 and "cex_small_cap:hold" in st["positions"], "FALHOU: não pode haver reset"
    assert pump_watch._maybe_close_review_cycle(st, now + 60) is None, "FALHOU: ciclo #2 acabou de abrir"

    # ciclo #2 fecha por tempo (20 dias), mesmo sem trades
    r2 = pump_watch._maybe_close_review_cycle(st, now + config.PUMP_WATCH_REVIEW_AFTER_DAYS * 86400 + 1)
    assert r2 and r2["number"] == 2 and r2["trigger"] == "days" and r2["num_trades"] == 0
    assert len(st["review_history"]) == 2
    msg = telegram_alert.format_pump_watch_review(st, r1, 0.9)
    assert "review cycle 1 closed" in msg and "HOLD" in msg, msg
    print("✅ Pump Watch — ciclos de revisão contínuos: fecham aos 10 trades ou 20 dias, "
          "guardam o resumo e abrem o ciclo seguinte sem reset")


def _lsk_like_chart():
    """Forma real da LSK (dados horários CoinGecko 7-10 set 2026): ~60h de base plana a ~$0,103
    com volume-24h ~$1,2M, depois 12h em que o volume-24h sobe para ~$4-5M com o preço só +10%."""
    prices, volumes = [], []
    for i in range(60):
        prices.append(0.1030 + (0.0004 if i % 2 else -0.0003))
        volumes.append(1_200_000 + (i % 5) * 10_000)
    for k in range(12):
        prices.append(0.1035 * (1 + 0.009 * (k + 1)) * (0.998 if k % 3 == 2 else 1.0))
        volumes.append(1_500_000 + 300_000 * (k + 1))
    return fake_chart(prices, volumes)


def _scenario_lsk_like_volume_awakening():
    sig = pump_watch._compute_accumulation_signal(**{k: v for k, v in zip(("prices", "volumes"), (
        _lsk_like_chart()["prices"], _lsk_like_chart()["volumes"]))})
    assert sig["volume_surge"] >= config.PUMP_WATCH_MIN_VOLUME_SURGE, sig
    assert sig["obv_score"] >= config.PUMP_WATCH_MIN_OBV_SCORE, sig
    assert config.PUMP_WATCH_MIN_PRICE_MOVE_PCT <= sig["price_change_pct"] <= config.PUMP_WATCH_MAX_PRICE_MOVE_PCT, sig
    flat = pump_watch._compute_accumulation_signal(**dict(zip(("prices", "volumes"), (
        _flat_no_volume_chart()["prices"], _flat_no_volume_chart()["volumes"]))))
    assert flat["volume_surge"] < config.PUMP_WATCH_MIN_VOLUME_SURGE, flat
    print(f"✅ Despertar do volume (forma LSK) OK — surge {sig['volume_surge']:.1f}x, OBV {sig['obv_score']:.2f}, "
          f"preço {sig['price_change_pct']:+.1%}: o sinal dispara; moeda parada fica de fora (surge {flat['volume_surge']:.1f}x)")


def _scenario_calm_filter_shortlist(eur_rate):
    """Caso LSK: o shortlist era o top-10 por turnover absoluto — moedas já a subir (turnover
    alto) empurravam para fora a que estava a acumular. Agora só moedas calmas entram."""
    original_fetch_chart = cg.fetch_market_chart
    requested = []
    try:
        def fake_fetch_market_chart(coin_id, days):
            requested.append(coin_id)
            return _lsk_like_chart() if coin_id == "lsk" else _already_pumped_chart()
        cg.fetch_market_chart = fake_fetch_market_chart
        hot = [dict(fake_candidate(f"HOT{i}", 1.0, turnover=3.0), chg_24h=45.0, chg_7d=80.0) for i in range(10)]
        lsk = dict(fake_candidate("LSK", 0.115, turnover=0.14, cid="lsk"), chg_24h=11.0, chg_7d=11.6)
        picks = pump_watch._scan_accumulation_candidates(hot + [lsk], held_ids=set(), slots_free=3)
        assert [c["id"] for c in picks] == ["lsk"], [c["id"] for c in picks]
        assert not any(r.startswith("hot") for r in requested), (
            "FALHOU: moedas já a subir não deviam gastar chamadas de histórico no shortlist")
    finally:
        cg.fetch_market_chart = original_fetch_chart
    print("✅ Shortlist só com moedas calmas OK — 10 moedas já a subir (turnover 3,0) não empurram a "
          "LSK (turnover 0,14, preço +11%) para fora; nenhuma chamada de histórico desperdiçada nelas")


_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>Lisk is Shutting Down Its Blockchain After 10 Years - Yahoo Finance</title><link>https://x/1</link>
<pubDate>Tue, 25 Aug 2026 14:00:00 GMT</pubDate><source url="https://y">Yahoo Finance</source></item>
<item><title>Lisk proposes to burn 100 million LSK from treasury - CoinDesk</title><link>https://x/2</link>
<pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate><source url="https://c">CoinDesk</source></item>
<item><title>Lisk Price Now: Instant SAND to USD Price - Kabul University</title><link>https://x/3</link>
<pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate></item>
<item><title>How to buy cheap flights - Travel</title><link>https://x/4</link>
<pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate></item>
<item><title>Lisk Chain Shutdown: Bridge LSK by October 21 - CryptoTicker</title><link>https://x/5</link>
<pubDate>Thu, 10 Sep 2026 09:00:00 GMT</pubDate></item>
<item><title>Lisk partners with old exchange - 2025</title><link>https://x/6</link>
<pubDate>Mon, 01 Jan 2024 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def _scenario_news_parsing():
    from screener import sources_news
    items = sources_news.parse_rss(_SAMPLE_RSS)
    assert len(items) == 6 and items[0]["ts"] and items[0]["source"] == "Yahoo Finance"
    original = sources_news._get_text
    try:
        sources_news._get_text = lambda *a, **k: _SAMPLE_RSS
        now = 1789400000  # ~ 11/09/2026
        heads = sources_news.fetch_headlines("Lisk", "LSK", lookback_days=30, now=now)
        titles = [h["title"] for h in heads]
        assert not any("cheap flights" in t for t in titles), "FALHOU: manchete sem a moeda devia ser ignorada"
        assert not any("2025" in t for t in titles), "FALHOU: manchete fora da janela devia ser ignorada"
        assert titles[0].startswith("Lisk Chain Shutdown"), "FALHOU: devia vir a mais recente primeiro"
    finally:
        sources_news._get_text = original
    print(f"✅ Notícias OK — RSS lido, {len(heads)} manchetes relevantes (ruído e manchetes antigas filtradas)")


def _scenario_catalysts(eur_rate):
    from screener import catalysts, sources_news, telegram_alert
    heads_lsk = [
        {"title": "Lisk is Shutting Down Its Blockchain After 10 Years", "ts": 1788000000, "source": "Yahoo"},
        {"title": "Lisk proposes to burn 100 million LSK from treasury", "ts": 1788100000, "source": "CoinDesk"},
    ]
    cat = catalysts.classify(heads_lsk)
    assert cat["tags"] == ["restructuring", "supply_cut"] and abs(cat["score"] - 0.9) < 1e-9 and not cat["blocking"], cat
    hacked = catalysts.classify([{"title": "Foo protocol exploited, $20M drained", "ts": 1, "source": "x"}])
    assert hacked["blocking"], hacked
    risk = catalysts.classify([{"title": "3 Altcoins Decline as Binance Flags Delisting Risk", "ts": 1, "source": "x"}])
    assert risk["tags"] == ["exchange_risk"] and not risk["blocking"], "FALHOU: risco de delisting não deve bloquear"

    original_fetch_chart = cg.fetch_market_chart
    original_heads = sources_news.fetch_headlines
    original_venue, original_contract = cg.fetch_top_venue, cg.fetch_contract_address
    config.CATALYST_ENABLED = True
    try:
        cg.fetch_market_chart = lambda coin_id, days: _lsk_like_chart()
        cg.fetch_top_venue = lambda coin_id: None
        cg.fetch_contract_address = lambda coin_id: None
        def fake_heads(name, symbol, lookback_days=None, now=None):
            if symbol == "LSK":
                return heads_lsk
            if symbol == "HACK":
                return [{"title": "Hack exploited: HACK bridge drained", "ts": 1, "source": "x"}]
            return []
        sources_news.fetch_headlines = fake_heads
        cands = [dict(fake_candidate("LSK", 0.115, turnover=0.14, cid="lisk"), name="Lisk"),
                 dict(fake_candidate("HACK", 1.0, turnover=0.50, cid="hackcoin"), name="HackCoin")]
        state = pump_watch._default_state()
        actions = pump_watch._check_entries(state, cands, eur_rate, now=1789400000.0)
        assert [a["symbol"] for a in actions] == ["LSK"], [a["symbol"] for a in actions]
        pos = state["positions"]["cex_small_cap:lisk"]
        assert pos["entry_catalyst_tags"] == ["restructuring", "supply_cut"] and pos["entry_volume_surge"] >= 2
        msg = telegram_alert.format_pump_watch_message(state, actions, eur_rate)
        assert "📰 restructuring, supply cut" in msg, msg
    finally:
        cg.fetch_market_chart = original_fetch_chart
        sources_news.fetch_headlines = original_heads
        cg.fetch_top_venue, cg.fetch_contract_address = original_venue, original_contract
        config.CATALYST_ENABLED = False
    print("✅ Catalisadores OK — LSK etiquetada (queima de oferta + reestruturação, score 0,9) e comprada; "
          "moeda com hack recente bloqueada; risco de delisting só registado; 📰 visível no alerta")


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
