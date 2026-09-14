"""
Teste offline da pipeline, com dados falsos que imitam a forma real das APIs.
Não faz nenhuma chamada de rede — serve para validar a lógica (scoring, filtros,
dedup, formatação da mensagem) antes de correr contra a internet real no GitHub Actions.

Corre com: python -m tests.test_pipeline_offline
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import scoring, state as state_mod, telegram_alert, config  # noqa: E402


def fake_cex_candidate(symbol, chg_1h, chg_24h, chg_7d, mcap=20_000_000, vol=6_000_000):
    return {
        "tier": "cex_small_cap",
        "id": symbol.lower(),
        "symbol": symbol,
        "name": f"{symbol} Coin",
        "price_usd": 0.42,
        "market_cap": mcap,
        "market_cap_rank": 512,
        "volume_24h": vol,
        "turnover": vol / mcap,
        "chg_1h": chg_1h,
        "chg_24h": chg_24h,
        "chg_7d": chg_7d,
        "url": f"https://www.coingecko.com/en/coins/{symbol.lower()}",
        "security": {"checked": False, "safe": True, "notes": "CEX listada"},
    }


def fake_dex_candidate(symbol, chg_1h, chg_6h, liq, vol, boosted=False, safe=True, checked=True):
    return {
        "tier": "dex_micro_cap",
        "id": f"0xfake{symbol.lower()}",
        "network": "base",
        "symbol": symbol,
        "name": symbol,
        "price_usd": 0.0000123,
        "market_cap": 900_000,
        "liquidity_usd": liq,
        "volume_24h": vol,
        "chg_1h": chg_1h,
        "chg_6h": chg_6h,
        "chg_24h": chg_24h if (chg_24h := chg_1h * 3) else None,
        "pool_age_minutes": 180,
        "boosted": boosted,
        "url": f"https://dexscreener.com/base/0xfake{symbol.lower()}",
        "pool_address": f"0xfake{symbol.lower()}",
        "security": (
            {"checked": True, "safe": safe, "notes": "sem red flags óbvias" if safe else "honeypot"}
            if checked
            else {"checked": False, "safe": True, "notes": "sem dados GoPlus — não bloqueado, mas não confirmado"}
        ),
    }


def run():
    candidates = [
        fake_cex_candidate("ALPHA", chg_1h=3.2, chg_24h=18.5, chg_7d=40.0),   # deve pontuar alto
        fake_cex_candidate("BETA", chg_1h=0.1, chg_24h=1.0, chg_7d=-2.0),     # deve pontuar baixo
        fake_dex_candidate("GAMMA", chg_1h=25.0, chg_6h=60.0, liq=40_000, vol=180_000, boosted=True),  # alto
        fake_dex_candidate("DELTA", chg_1h=5.0, chg_6h=8.0, liq=16_000, vol=22_000),                    # médio/baixo
        fake_dex_candidate("SCAM", chg_1h=90.0, chg_6h=200.0, liq=50_000, vol=300_000, safe=False),     # deve ser ELIMINADO
        # Autoanálise 2026-08-30: candidato sem dados de segurança (GoPlus falhou/sem info) —
        # antes passava com só um desconto de 10% no score; TRUMPSTACY tinha exatamente esta
        # nota e mesmo assim comprou com score 90, rugou -99.5%. Agora deve ser eliminado.
        fake_dex_candidate("GHOST", chg_1h=90.0, chg_6h=200.0, liq=50_000, vol=300_000, checked=False),
    ]

    ranked = scoring.score_and_rank(candidates)
    ids_ranked = [c["symbol"] for c in ranked]

    print("--- Ranking (após filtros de segurança e limiar de score) ---")
    for c in ranked:
        print(f"{c['symbol']:8s} tier={c['tier']:15s} score={c['score']}")

    assert "SCAM" not in ids_ranked, "FALHOU: token com honeypot confirmado não devia passar o gate de segurança"
    assert "GHOST" not in ids_ranked, "FALHOU: token sem verificação de segurança (GoPlus falhou) não devia passar o gate"
    assert ids_ranked.index("ALPHA") < ids_ranked.index("BETA") if "BETA" in ids_ranked else True
    assert "GAMMA" in ids_ranked, "FALHOU: GAMMA devia ter score suficiente para aparecer"

    # --- teste de dedup/estado ---
    with tempfile.TemporaryDirectory() as tmp:
        config.STATE_FILE = os.path.join(tmp, "state.json")
        state = {}
        first_pass, state = state_mod.filter_new_or_accelerating(ranked, state)
        state_mod.save_state(state)
        assert len(first_pass) == len(ranked), "FALHOU: primeira corrida devia alertar tudo o que passou o score"

        # segunda "corrida" imediata com os mesmos scores -> nada de novo (cooldown ativo)
        second_pass, state = state_mod.filter_new_or_accelerating(ranked, state)
        assert len(second_pass) == 0, "FALHOU: corrida repetida sem aceleração não devia re-alertar"

        # simula aceleração forte -> deve voltar a alertar (usa o candidato com mais margem
        # para subir, para não saturar no teto de 100)
        base_candidate = min(ranked, key=lambda c: c["score"])
        accelerated = [dict(base_candidate)]
        accelerated[0]["score"] = min(100, accelerated[0]["score"] + 20)
        third_pass, state = state_mod.filter_new_or_accelerating(accelerated, state)
        assert len(third_pass) == 1, "FALHOU: aceleração de score devia re-disparar o alerta"

    # --- teste de formatação da mensagem ---
    cex_top = [c for c in ranked if c["tier"] == "cex_small_cap"][:config.TOP_N_PER_TIER]
    dex_top = [c for c in ranked if c["tier"] == "dex_micro_cap"][:config.TOP_N_PER_TIER]
    msg = telegram_alert.build_message(cex_top, dex_top)
    assert "not financial advice" in msg
    assert len(msg) < 4096, "mensagem única ainda cabe num só envio Telegram (bom para o caso normal)"

    print("\n--- Mensagem Telegram simulada ---\n")
    print(msg)

    print("\n✅ Todos os testes offline passaram.")


if __name__ == "__main__":
    run()
