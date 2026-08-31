"""
Teste offline do comando /preco (cotação em tempo real de um símbolo já visto pelo
screener) — pedido do Ricardo 2026-08-31, para poder acompanhar uma moeda sem esperar
pelo relatório de 2h em 2h. Não faz chamadas de rede reais: as funções de fetch são
substituídas por versões falsas, tal como em test_portfolio_offline.py.

Corre com: python -m tests.test_telegram_bot_offline
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import config, portfolio, sources_coingecko as cg, sources_dexscreener as ds  # noqa: E402
from screener import state as state_mod  # noqa: E402
from screener import telegram_bot  # noqa: E402


def run():
    with tempfile.TemporaryDirectory() as tmp:
        config.STATE_FILE = os.path.join(tmp, "state.json")
        config.PORTFOLIO_STATE_FILE = os.path.join(tmp, "portfolio_state.json")

        # --- símbolo visto recentemente num alerta (data/state.json), camada CEX ---
        state = {"cex_small_cap:zora": {"last_alert_ts": 1.0, "last_score": 99.2, "symbol": "ZORA"}}
        state_mod.save_state(state)

        cg.fetch_by_ids = lambda ids: {
            "zora": {"id": "zora", "name": "Zora", "symbol": "ZORA", "price_usd": 0.0091,
                     "chg_1h": 3.2, "chg_24h": -1.5}
        }
        reply = telegram_bot._handle_preco_command("zora")
        assert "Zora" in reply and "0.0091" in reply, f"FALHOU: resposta inesperada para ZORA: {reply}"
        assert "+3.2%" in reply and "-1.5%" in reply, f"FALHOU: variações em falta na resposta: {reply}"
        print("✅ /preco OK — símbolo da camada CEX visto em state.json devolve cotação em tempo real")

        # case-insensitive e com espaço extra
        reply_lower = telegram_bot._handle_preco_command(" zOrA ")
        assert "Zora" in reply_lower, "FALHOU: /preco devia ser insensível a maiúsculas/minúsculas e espaços"
        print("✅ /preco OK — símbolo é insensível a maiúsculas/minúsculas")

        # --- símbolo só existe numa posição aberta do desafio, não em state.json (pode já ter
        # sido limpo pelo STATE_MAX_AGE_HOURS) ---
        pf_state = portfolio._default_state()
        pf_state["status"] = "active"
        pf_state["positions"]["dex_micro_cap:0xfaketoken"] = {
            "tier": "dex_micro_cap", "id": "0xfaketoken", "symbol": "FAKE", "qty": 100,
            "entry_price_eur": 0.01, "cost_eur": 25.0, "last_price_eur": 0.01,
        }
        portfolio.save_portfolio(pf_state)

        ds.fetch_market_data_for_addresses = lambda addrs, **k: [
            {"tier": "dex_micro_cap", "id": "0xfaketoken", "symbol": "FAKE", "name": "FakeCoin",
             "price_usd": 0.0123, "chg_1h": 10.0, "chg_24h": 40.0}
        ]
        reply_pos = telegram_bot._handle_preco_command("fake")
        assert "FakeCoin" in reply_pos and "0.0123" in reply_pos, (
            f"FALHOU: /preco devia encontrar um símbolo só presente numa posição aberta: {reply_pos}"
        )
        print("✅ /preco OK — encontra um símbolo só presente numa posição aberta (não em state.json)")

        # --- símbolo desconhecido ---
        reply_unknown = telegram_bot._handle_preco_command("naoexiste")
        assert "Não encontrei" in reply_unknown, f"FALHOU: resposta inesperada para símbolo desconhecido: {reply_unknown}"
        print("✅ /preco OK — símbolo nunca visto devolve mensagem clara em vez de rebentar")

        # --- sem argumento ---
        reply_empty = telegram_bot._handle_preco_command("")
        assert "Uso:" in reply_empty, "FALHOU: /preco sem argumento devia explicar a utilização"
        print("✅ /preco OK — sem argumento explica a utilização em vez de procurar símbolo vazio")

    print("\n✅ Todos os testes offline do /preco passaram.")


if __name__ == "__main__":
    run()
