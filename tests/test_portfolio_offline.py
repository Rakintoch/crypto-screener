"""
Teste offline do motor de portfólio virtual. Não faz chamadas de rede — simula
diretamente o estado e os candidatos, para validar a aritmética de entradas, saídas,
take-profit, stop-loss, decaimento de score e a liquidação forçada ao fim de 10 dias.

Corre com: python -m tests.test_portfolio_offline
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import changelog, config, lessons, playbook, portfolio, telegram_alert  # noqa: E402


def fake_candidate(symbol, score, price_usd, tier="cex_small_cap", cid=None, liquidity_usd=None):
    return {
        "tier": tier,
        "id": cid or symbol.lower(),
        "symbol": symbol,
        "name": symbol,
        "price_usd": price_usd,
        "market_cap": 20_000_000,
        "market_cap_rank": 400,
        "volume_24h": 5_000_000,
        "turnover": 0.25,
        "chg_1h": 5.0,
        "chg_24h": 20.0,
        "chg_7d": 30.0,
        "chg_6h": 15.0,
        "boosted": False,
        "liquidity_usd": liquidity_usd,
        "url": "https://example.com",
        "score": score,
        "security": {"checked": True, "safe": True, "notes": "ok"},
    }


def run():
    # o trailing stop usa time.sleep() real entre verificações — no teste offline anulamos
    # isso para o teste correr em milissegundos em vez de minutos.
    original_sleep = portfolio.time.sleep
    portfolio.time.sleep = lambda seconds: None
    try:
        _run_scenarios()
    finally:
        portfolio.time.sleep = original_sleep


def _run_scenarios():
    with tempfile.TemporaryDirectory() as tmp:
        config.PORTFOLIO_STATE_FILE = os.path.join(tmp, "portfolio_state.json")
        config.LESSONS_FILE = os.path.join(tmp, "lessons.json")  # isola das lições reais do repo
        config.WINS_FILE = os.path.join(tmp, "wins.json")  # isola das vitórias reais do repo
        config.CHANGELOG_FILE = os.path.join(tmp, "changelog.json")  # isola do changelog real do repo
        eur_rate = 0.9  # taxa fixa para o teste ser determinístico

        # --- Corrida 1: sem posições, dois candidatos elegíveis para compra ---
        candidates = [
            fake_candidate("ALPHA", score=80, price_usd=1.0),
            fake_candidate("BETA", score=70, price_usd=2.0),
        ]
        state, actions, final_report = portfolio.run_portfolio_cycle(candidates, eur_rate)

        assert state["status"] == "active", "FALHOU: primeira compra devia iniciar o desafio"
        assert state["start_ts"] is not None
        assert len(state["positions"]) == 2, f"FALHOU: esperava 2 posições abertas, tem {len(state['positions'])}"
        assert len([a for a in actions if a["action"] == "buy"]) == 2

        equity_after_buys = state["cash_eur"] + sum(
            p["qty"] * p["last_price_eur"] for p in state["positions"].values()
        )
        assert abs(equity_after_buys - config.STARTING_BALANCE_EUR) < 0.01, (
            "FALHOU: equity logo após compras devia ser igual ao saldo inicial (sem custos de transação)"
        )
        print(f"✅ Corrida 1 OK — {len(state['positions'])} posições abertas, "
              f"cash restante {state['cash_eur']:.2f} EUR")

        # --- Corrida 2: ALPHA sobe 25% (dispara take-profit em CEX, limiar 20%) ---
        # Isolamos este cenário do trailing stop (testado à parte, na Corrida 2b) para validar
        # só a matemática base do take-profit.
        alpha_key = "cex_small_cap:alpha"
        alpha_pos = state["positions"][alpha_key]
        candidates_run2 = [fake_candidate("BETA", score=70, price_usd=2.0)]  # BETA mantém-se, sem novo sinal
        # simulamos diretamente a reavaliação de preço da ALPHA para isolar o teste do take-profit
        state["positions"][alpha_key]["last_price_eur"] = alpha_pos["entry_price_eur"] * 1.25
        state["positions"][alpha_key]["last_score"] = 80
        portfolio.save_portfolio(state)

        # força uma corrida que já teria reavaliado preços (usamos os mesmos preços simulados)
        import screener.sources_coingecko as cg
        import screener.sources_dexscreener as ds

        original_fetch_by_ids = cg.fetch_by_ids
        cg.fetch_by_ids = lambda ids: {
            "alpha": {"id": "alpha", "price_usd": (alpha_pos["entry_price_eur"] * 1.25) / eur_rate,
                      "chg_1h": 5, "chg_24h": 25, "chg_7d": 30, "turnover": 0.3, "market_cap": 20_000_000}
        }
        original_trailing_enabled = config.TRAILING_STOP_ENABLED
        config.TRAILING_STOP_ENABLED = False
        try:
            state, actions2, final_report2 = portfolio.run_portfolio_cycle(candidates_run2, eur_rate)
        finally:
            cg.fetch_by_ids = original_fetch_by_ids
            config.TRAILING_STOP_ENABLED = original_trailing_enabled

        sells = [a for a in actions2 if a["action"] == "sell"]
        assert any(a["symbol"] == "ALPHA" for a in sells), "FALHOU: ALPHA devia ter sido vendida por take-profit"
        assert alpha_key not in state["positions"], "FALHOU: posição ALPHA devia ter sido removida"
        alpha_trade = next(t for t in state["closed_trades"] if t["symbol"] == "ALPHA")
        assert alpha_trade["pnl_pct"] > 20, f"FALHOU: pnl esperado >20%, obtido {alpha_trade['pnl_pct']:.1f}%"
        print(f"✅ Corrida 2 OK — ALPHA fechada com {alpha_trade['pnl_pct']:+.1f}% "
              f"({alpha_trade['exit_reason']})")

        # take-profit direto com lucro -> deve gerar uma "vitória" no playbook
        alpha_wins = [w for w in playbook._load() if w["symbol"] == "ALPHA"]
        assert len(alpha_wins) == 1, f"FALHOU: esperava exatamente 1 vitória para ALPHA, obtido {len(alpha_wins)}"
        assert alpha_wins[0]["categoria"] == "take-profit direto", (
            f"FALHOU: categoria esperada 'take-profit direto', obtido '{alpha_wins[0]['categoria']}'"
        )
        print(f"✅ Corrida 2 (playbook) OK — vitória de ALPHA registada automaticamente: "
              f"\"{alpha_wins[0]['nota'][:70]}...\"")

        # --- Corrida 2b: trailing stop — GAMMA ultrapassa o alvo, sobe mais, depois reverte ---
        gamma_key = "dex_micro_cap:gammaaddr"
        state["positions"][gamma_key] = {
            "tier": "dex_micro_cap", "id": "gammaaddr", "symbol": "GAMMA", "network": "solana",
            "url": "https://example.com", "qty": 100.0, "entry_price_eur": 1.0,
            "entry_ts": time.time(), "cost_eur": 100.0, "last_price_eur": 1.0,
            "last_score": 80, "missed_updates": 0,
        }
        portfolio.save_portfolio(state)

        # sequência de preços vista durante a vigilância de 5 min: cruza bem o alvo (+45%, para
        # evitar problemas de arredondamento em vírgula flutuante mesmo em cima do limiar dos
        # 40%), sobe mais até um pico (+70%), depois cai bem mais de 7% desse pico -> deve
        # vender no pico-7%
        gamma_prices = iter([1.45, 1.55, 1.70, 1.55])  # 1.55 é -8.8% do pico 1.70 -> dispara
        original_fetch_addrs = ds.fetch_market_data_for_addresses
        ds.fetch_market_data_for_addresses = lambda addrs, *a, **k: [
            {"id": "gammaaddr", "price_usd": next(gamma_prices) / eur_rate}
        ]
        # BETA continua aberta (cex) — mantemos o seu preço estável e mockado, para o teste
        # continuar 100% offline (sem chamadas de rede reais) também nesta corrida.
        cg.fetch_by_ids = lambda ids: {
            "beta": {"id": "beta", "price_usd": 2.0, "chg_1h": 5, "chg_24h": 20, "chg_7d": 30,
                     "turnover": 0.3, "market_cap": 20_000_000}
        }
        try:
            state, actions2b, _ = portfolio.run_portfolio_cycle([], eur_rate)
        finally:
            ds.fetch_market_data_for_addresses = original_fetch_addrs
            cg.fetch_by_ids = original_fetch_by_ids

        sells_2b = [a for a in actions2b if a["action"] == "sell"]
        assert any(a["symbol"] == "GAMMA" for a in sells_2b), "FALHOU: GAMMA devia ter sido vendida pelo trailing stop"
        gamma_trade = next(t for t in state["closed_trades"] if t["symbol"] == "GAMMA")
        assert "trailing stop" in gamma_trade["exit_reason"], (
            f"FALHOU: esperava saída por trailing stop, obtido '{gamma_trade['exit_reason']}'"
        )
        assert 54 < gamma_trade["pnl_pct"] < 56, (
            f"FALHOU: esperava apanhar a maior parte do pico (~+55%), obtido {gamma_trade['pnl_pct']:.1f}%"
        )
        print(f"✅ Corrida 2b OK — GAMMA fechada com {gamma_trade['pnl_pct']:+.1f}% pelo trailing stop "
              f"(capturou mais do que o alvo fixo de +40%)")

        # trailing stop com lucro extra -> deve gerar uma "vitória" com a categoria correta
        gamma_wins = [w for w in playbook._load() if w["symbol"] == "GAMMA"]
        assert len(gamma_wins) == 1, f"FALHOU: esperava exatamente 1 vitória para GAMMA, obtido {len(gamma_wins)}"
        assert gamma_wins[0]["categoria"] == "trailing stop apanhou subida extra após o alvo", (
            f"FALHOU: categoria esperada de trailing stop, obtido '{gamma_wins[0]['categoria']}'"
        )

        # com 2 vitórias já registadas (ALPHA, GAMMA), o "modus operandi" já deve produzir
        # um resumo com números, não a mensagem de "ainda não há vitórias suficientes"
        modus = playbook.build_modus_operandi()
        assert "Modus operandi" in modus, "FALHOU: build_modus_operandi() devia ter produzido um resumo"
        assert "ainda não há vitórias" not in modus.lower(), (
            "FALHOU: com 2 vitórias já registadas, não devia cair no caso de 'sem dados'"
        )
        print(f"✅ Corrida 2b (playbook) OK — vitória de GAMMA registada e /modus já produz um resumo:\n"
              f"{modus}")

        # --- Corrida 2c: DELTA entra e depois dispara stop-loss -> deve gerar uma "lição" ---
        candidates_2c = [fake_candidate("DELTA", score=72, price_usd=1.0, tier="dex_micro_cap",
                                         cid="deltaaddr", liquidity_usd=18_000)]
        cg.fetch_by_ids = lambda ids: {
            "beta": {"id": "beta", "price_usd": 2.0, "chg_1h": 5, "chg_24h": 20, "chg_7d": 30,
                     "turnover": 0.3, "market_cap": 20_000_000}
        }
        ds.fetch_market_data_for_addresses = lambda addrs, *a, **k: []
        equity_before_delta = portfolio._equity(state)
        state, actions2c, _ = portfolio.run_portfolio_cycle(candidates_2c, eur_rate)
        assert any(a["symbol"] == "DELTA" for a in actions2c if a["action"] == "buy"), (
            "FALHOU: DELTA devia ter sido comprada (score acima do mínimo, slot livre)"
        )
        delta_key = "dex_micro_cap:deltaaddr"
        assert state["positions"][delta_key]["entry_score"] == 72, (
            "FALHOU: snapshot de entry_score não foi guardado na posição"
        )
        # Autoanálise 2026-08-30: dex_micro_cap agora usa um tamanho de posição menor
        # (12% do equity) do que cex_small_cap (25%), para limitar o estrago de rugs
        # individuais — confirma que a compra de DELTA (dex) respeitou essa fatia menor.
        expected_delta_cost = equity_before_delta * config.POSITION_SIZE_PCT_BY_TIER["dex_micro_cap"]
        assert abs(state["positions"][delta_key]["cost_eur"] - expected_delta_cost) < 0.05, (
            f"FALHOU: DELTA (dex_micro_cap) devia usar ~12% do equity ({expected_delta_cost:.2f}€), "
            f"usou {state['positions'][delta_key]['cost_eur']:.2f}€"
        )

        # crash direto: -35% (abaixo do stop-loss de -20% para dex_micro_cap)
        ds.fetch_market_data_for_addresses = lambda addrs, *a, **k: [
            {"id": "deltaaddr", "price_usd": 0.65 / eur_rate}
        ]
        state, actions2c_b, _ = portfolio.run_portfolio_cycle([], eur_rate)
        sells_2c = [a for a in actions2c_b if a["action"] == "sell" and a["symbol"] == "DELTA"]
        assert sells_2c, "FALHOU: DELTA devia ter sido vendida por stop-loss"
        assert delta_key not in state["positions"]

        all_lessons = lessons._load()
        delta_lessons = [l for l in all_lessons if l["symbol"] == "DELTA"]
        assert len(delta_lessons) == 1, f"FALHOU: esperava exatamente 1 lição para DELTA, obtido {len(delta_lessons)}"
        assert delta_lessons[0]["pnl_pct"] < 0
        assert delta_lessons[0]["entry_score"] == 72
        assert delta_lessons[0]["entry_liquidity_usd"] == 18_000
        assert "categoria" in delta_lessons[0] and delta_lessons[0]["categoria"]
        # Autoanálise 2026-09-10: os componentes brutos do score na entrada (camada DEX) agora
        # ficam guardados no trade, não só o score final combinado — necessário para uma futura
        # autoanálise conseguir decompor qual sinal específico falhou, em vez de só conseguir
        # provar que o score final, como um todo, não prevê o resultado.
        assert delta_lessons[0]["entry_chg_1h"] == candidates_2c[0]["chg_1h"], (
            "FALHOU: entry_chg_1h (camada DEX) não foi guardado no snapshot da posição/lição"
        )
        assert delta_lessons[0]["entry_chg_6h"] == candidates_2c[0]["chg_6h"], (
            "FALHOU: entry_chg_6h (camada DEX) não foi guardado no snapshot da posição/lição"
        )
        expected_vol_liq = candidates_2c[0]["volume_24h"] / candidates_2c[0]["liquidity_usd"]
        assert abs(delta_lessons[0]["entry_vol_liq_ratio"] - expected_vol_liq) < 0.001, (
            "FALHOU: entry_vol_liq_ratio não foi calculado/guardado corretamente"
        )
        assert delta_lessons[0]["entry_boosted"] == candidates_2c[0]["boosted"], (
            "FALHOU: entry_boosted (camada DEX) não foi guardado no snapshot da posição/lição"
        )
        print(f"✅ Corrida 2c OK — DELTA fechada com {delta_lessons[0]['pnl_pct']:+.1f}% e uma lição foi "
              f"registada automaticamente: \"{delta_lessons[0]['licao'][:70]}...\" "
              f"(sinais brutos de entrada preservados na lição)")

        # --- Corrida 2c-bis: teto de seleção por score (config.SELECTION_SCORE_CEILING) —
        # autoanálise 2026-09-10 (79 trades do 2º desafio): o score não previu o resultado
        # (correlação -0,019) e o escalão 90-100 foi o PIOR em € líquidos de todos — por isso,
        # acima do teto, dois candidatos elegíveis ao mesmo tempo deixam de ser desempatados
        # por quem subiu mais (score bruto) e passam a ser desempatados por liquidez.
        fresh_state = portfolio._default_state()
        fresh_state["status"] = "active"
        fresh_state["start_ts"] = time.time()
        fresh_state["end_ts"] = time.time() + 999_999
        # ocupa todos os slots menos 1, para forçar o desempate entre os dois candidatos abaixo
        for i in range(config.MAX_CONCURRENT_POSITIONS - 1):
            fresh_state["positions"][f"cex_small_cap:dummy{i}"] = {
                "tier": "cex_small_cap", "id": f"dummy{i}", "symbol": f"DUMMY{i}", "qty": 1.0,
                "entry_price_eur": 1.0, "entry_ts": time.time(), "cost_eur": 1.0,
                "last_price_eur": 1.0, "last_score": 80, "missed_updates": 0,
            }

        candidate_hot = fake_candidate("HOT", score=99.4, price_usd=1.0, cid="hot", liquidity_usd=5_000)
        candidate_solid = fake_candidate("SOLID", score=91.0, price_usd=1.0, cid="solid", liquidity_usd=500_000)
        for c in (candidate_hot, candidate_solid):
            c["_eur_rate"] = eur_rate
        ranked = sorted([candidate_hot, candidate_solid], key=lambda c: c["score"], reverse=True)

        buy_actions = portfolio._check_entries(fresh_state, ranked, time.time())
        assert len(buy_actions) == 1, f"FALHOU: só devia sobrar 1 slot livre, comprou {len(buy_actions)}"
        assert buy_actions[0]["symbol"] == "SOLID", (
            f"FALHOU: acima do teto de {config.SELECTION_SCORE_CEILING}, devia desempatar por liquidez "
            f"(SOLID, $500k) em vez do score bruto mais alto (HOT, $5k liquidez) — comprou "
            f"{buy_actions[0]['symbol']}"
        )
        print(f"✅ Corrida 2c-bis OK — teto de seleção por score: entre HOT (score 99.4, $5k liquidez) e "
              f"SOLID (score 91.0, $500k liquidez), com 1 só slot livre, SOLID foi o escolhido "
              f"(desempate por liquidez acima do teto de {config.SELECTION_SCORE_CEILING})")

        # --- Registo de changelog: uma "mudança" autoanalisada fica pendente de anúncio até
        # ser marcada como tal, e depois aparece no histórico (/mudancas) ---
        entry = changelog.record_change(
            titulo="Teste de changelog",
            motivo="motivo de teste",
            mudanca="mudança de teste",
            efeito_esperado="efeito de teste",
            num_trades_fechados_ate_aqui=len(state["closed_trades"]),
        )
        pending = changelog.pending_announcements()
        assert len(pending) == 1 and pending[0]["titulo"] == "Teste de changelog", (
            "FALHOU: mudança recém-registada devia aparecer como pendente de anúncio"
        )
        changelog.mark_announced([entry["ts"]])
        assert changelog.pending_announcements() == [], (
            "FALHOU: mudança já anunciada não devia continuar pendente"
        )
        assert "Teste de changelog" in changelog.format_recent_changes(), (
            "FALHOU: mudança já anunciada devia continuar a aparecer no histórico"
        )
        print("✅ Changelog OK — mudança registada, marcada como anunciada, e continua no histórico")

        # --- Corrida 2d: disjuntor de capital — se o equity cair para o limiar de proteção
        # (config.MAX_DRAWDOWN_HALT_PCT, 25% do saldo inicial), o bot deixa de abrir posições
        # novas, avisa uma única vez no Telegram, e não volta a avisar em corridas seguintes ---
        alert_calls = []
        original_send_telegram = telegram_alert.send_telegram_message
        telegram_alert.send_telegram_message = lambda msg: (alert_calls.append(msg), True)[1]
        try:
            beta_key = "cex_small_cap:beta"
            beta_value = state["positions"][beta_key]["qty"] * state["positions"][beta_key]["last_price_eur"]
            floor = state["starting_balance_eur"] * (1 + config.MAX_DRAWDOWN_HALT_PCT)
            # deixa o equity total 1€ abaixo do limiar, sem tocar no preço da BETA (para não
            # disparar o stop-loss dela e sujar este teste, que é só sobre o disjuntor de entradas)
            state["cash_eur"] = max(0.0, floor - beta_value - 1.0)
            portfolio.save_portfolio(state)

            equity_before = beta_value + state["cash_eur"]
            assert equity_before <= floor, (
                "FALHOU (preparação do teste): equity devia estar abaixo do limiar de proteção"
            )

            candidates_2d = [fake_candidate("EPSILON", score=90, price_usd=1.0)]
            cg.fetch_by_ids = lambda ids: {
                "beta": {"id": "beta", "price_usd": 2.0, "chg_1h": 5, "chg_24h": 20, "chg_7d": 30,
                         "turnover": 0.3, "market_cap": 20_000_000}
            }
            ds.fetch_market_data_for_addresses = lambda *a, **k: []
            state, actions2d, _ = portfolio.run_portfolio_cycle(candidates_2d, eur_rate)
            assert not any(a["action"] == "buy" for a in actions2d), (
                "FALHOU: disjuntor de capital ativo não devia permitir a compra de EPSILON"
            )
            assert state.get("capital_protection_active") is True, "FALHOU: proteção de capital devia ficar ativa"
            assert len(alert_calls) == 1, f"FALHOU: esperava exatamente 1 alerta, obtido {len(alert_calls)}"

            # corrida seguinte com o disjuntor já ativo -> continua a bloquear, mas não repete o aviso
            state, actions2d_b, _ = portfolio.run_portfolio_cycle(candidates_2d, eur_rate)
            assert not any(a["action"] == "buy" for a in actions2d_b)
            assert len(alert_calls) == 1, (
                "FALHOU: o alerta de proteção de capital não devia repetir-se em corridas seguintes"
            )
        finally:
            telegram_alert.send_telegram_message = original_send_telegram

        print(f"✅ Corrida 2d OK — disjuntor de capital acionado a {equity_before:.2f}€, "
              f"bloqueou novas entradas e avisou uma única vez")

        # --- Corrida 3: simula fim do desafio (10 dias) -> liquidação forçada de tudo ---
        state["end_ts"] = time.time() - 1  # já passou o prazo
        portfolio.save_portfolio(state)

        cg.fetch_by_ids = lambda ids: {}
        ds.fetch_market_data_for_addresses = lambda *a, **k: []
        state, actions3, final_report = portfolio.run_portfolio_cycle([], eur_rate)

        assert state["status"] == "finished", "FALHOU: estado devia ficar 'finished' após o prazo"
        assert not state["positions"], "FALHOU: não deviam sobrar posições abertas após liquidação forçada"
        assert final_report is not None, "FALHOU: devia ter sido gerado um relatório final"
        assert abs(final_report["final_balance_eur"] - state["cash_eur"]) < 0.01

        print(f"✅ Corrida 3 OK — desafio finalizado. Saldo final: {final_report['final_balance_eur']:.2f} EUR "
              f"({final_report['pnl_pct']:+.1f}%)")

        # corrida extra após o fim: não deve fazer mais nada (idempotência)
        state_after, actions4, report4 = portfolio.run_portfolio_cycle([], eur_rate)
        assert state_after["status"] == "finished"
        assert actions4 == [] and report4 is None
        print("✅ Corrida 4 OK — nenhuma ação após o desafio já ter terminado (idempotente)")

        # --- Corrida 5: ficheiro de estado corrompido (ex: marcadores de conflito do git
        # deixados por um "git pull --rebase --autostash" em conflito, incidente 2026-08-31)
        # não deve reiniciar o desafio silenciosamente — deve abortar a corrida sem gravar nada ---
        with open(config.PORTFOLIO_STATE_FILE, "w", encoding="utf-8") as f:
            f.write('{\n<<<<<<< Updated upstream\n"cash_eur": 1.0,\n=======\n"cash_eur": 2.0,\n>>>>>>> Stashed changes\n}\n')

        try:
            portfolio.load_portfolio()
            raise AssertionError("FALHOU: load_portfolio() devia ter levantado PortfolioStateCorrupted")
        except portfolio.PortfolioStateCorrupted:
            pass

        # run_portfolio_cycle() propaga a exceção (não a esconde) — é main.py/position_monitor.py
        # quem apanha PortfolioStateCorrupted, avisa no Telegram, e aborta sem gravar nada; aqui
        # simulamos exatamente esse contrato.
        corrupted_before = open(config.PORTFOLIO_STATE_FILE, encoding="utf-8").read()
        try:
            portfolio.run_portfolio_cycle([], eur_rate)
            raise AssertionError("FALHOU: run_portfolio_cycle devia ter propagado PortfolioStateCorrupted")
        except portfolio.PortfolioStateCorrupted:
            pass
        corrupted_after = open(config.PORTFOLIO_STATE_FILE, encoding="utf-8").read()
        assert corrupted_before == corrupted_after, (
            "FALHOU: run_portfolio_cycle não devia ter tocado no ficheiro corrompido "
            "(perderia a possibilidade de recuperação manual)"
        )
        print("✅ Corrida 5 OK — estado corrompido levanta PortfolioStateCorrupted (propagada por "
              "run_portfolio_cycle) sem sobrescrever o ficheiro nem reiniciar o desafio")

    print("\n✅ Todos os testes offline do portfólio passaram.")


if __name__ == "__main__":
    run()
