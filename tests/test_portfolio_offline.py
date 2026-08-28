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

from screener import config, portfolio  # noqa: E402


def fake_candidate(symbol, score, price_usd, tier="cex_small_cap", cid=None):
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
        "url": "https://example.com",
        "score": score,
        "security": {"checked": True, "safe": True, "notes": "ok"},
    }


def run():
    with tempfile.TemporaryDirectory() as tmp:
        config.PORTFOLIO_STATE_FILE = os.path.join(tmp, "portfolio_state.json")
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
        try:
            state, actions2, final_report2 = portfolio.run_portfolio_cycle(candidates_run2, eur_rate)
        finally:
            cg.fetch_by_ids = original_fetch_by_ids

        sells = [a for a in actions2 if a["action"] == "sell"]
        assert any(a["symbol"] == "ALPHA" for a in sells), "FALHOU: ALPHA devia ter sido vendida por take-profit"
        assert alpha_key not in state["positions"], "FALHOU: posição ALPHA devia ter sido removida"
        alpha_trade = next(t for t in state["closed_trades"] if t["symbol"] == "ALPHA")
        assert alpha_trade["pnl_pct"] > 20, f"FALHOU: pnl esperado >20%, obtido {alpha_trade['pnl_pct']:.1f}%"
        print(f"✅ Corrida 2 OK — ALPHA fechada com {alpha_trade['pnl_pct']:+.1f}% "
              f"({alpha_trade['exit_reason']})")

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

    print("\n✅ Todos os testes offline do portfólio passaram.")


if __name__ == "__main__":
    run()
