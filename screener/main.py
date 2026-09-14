"""Ponto de entrada: corre a pipeline completa. Desenhado para nunca rebentar por completo
por causa de UMA fonte de dados estar em baixo — cada camada é isolada em try/except."""
import sys
import traceback

from . import config
from . import sources_coingecko
from . import sources_geckoterminal
from . import sources_dexscreener
from . import security
from . import scoring
from . import state as state_mod
from . import telegram_alert
from . import fx
from . import portfolio
from . import pump_watch


def gather_candidates():
    candidates = []

    try:
        cex = sources_coingecko.fetch_small_cap_candidates()
        print(f"[main] CoinGecko: {len(cex)} candidatos small-cap")
        candidates.extend(cex)
    except Exception:
        print("[main] falha na camada CoinGecko:")
        traceback.print_exc()

    boosted_addresses = set()
    try:
        boosted_addresses = sources_dexscreener.fetch_boosted_addresses()
        print(f"[main] DexScreener: {len(boosted_addresses)} tokens em destaque (boosted)")
    except Exception:
        print("[main] falha a buscar boosts do DexScreener:")
        traceback.print_exc()

    try:
        gt = sources_geckoterminal.fetch_trending_and_new_pools(boosted_addresses)
        print(f"[main] GeckoTerminal: {len(gt)} pools trending/novas")
        candidates.extend(gt)
    except Exception:
        print("[main] falha na camada GeckoTerminal:")
        traceback.print_exc()

    try:
        seen_addresses = {c["id"].lower() for c in candidates if c["tier"] == "dex_micro_cap"}
        remaining_boosted = {a for a in boosted_addresses if a not in seen_addresses}
        ds = sources_dexscreener.fetch_market_data_for_boosted(remaining_boosted)
        print(f"[main] DexScreener: {len(ds)} candidatos boosted adicionais com dados de mercado")
        candidates.extend(ds)
    except Exception:
        print("[main] falha na camada DexScreener (market data):")
        traceback.print_exc()

    return candidates


def apply_security_checks(candidates):
    for c in candidates:
        if c["tier"] != "dex_micro_cap":
            continue
        try:
            c["security"] = security.check_token_security(c.get("network"), c["id"])
        except Exception as e:  # noqa: BLE001
            c["security"] = {"checked": False, "safe": True, "notes": f"error: {e}"}
    return candidates


def run_screener_alerts(ranked):
    """Alertas de descoberta (o ranking de momentum em si) — independente do portfólio virtual."""
    prev_state = state_mod.load_state()
    to_alert, new_state = state_mod.filter_new_or_accelerating(ranked, prev_state)
    state_mod.save_state(new_state)

    cex_top = [c for c in to_alert if c["tier"] == "cex_small_cap"][:config.TOP_N_PER_TIER]
    dex_top = [c for c in to_alert if c["tier"] == "dex_micro_cap"][:config.TOP_N_PER_TIER]

    if not cex_top and not dex_top:
        print("[main] nada de novo para alertar nesta corrida (tudo já alertado recentemente ou sem candidatos).")
        return

    message = telegram_alert.build_message(cex_top, dex_top)
    print("[main] mensagem de alerta a enviar:\n" + message)
    telegram_alert.send_telegram_message(message)


def run_portfolio_challenge(ranked):
    """Desafio de portfólio virtual de 10 dias — 100% simulado, dados reais."""
    if not config.PORTFOLIO_ENABLED:
        return

    try:
        eur_rate = fx.get_usd_to_eur_rate()
        state, actions, final_report = portfolio.run_portfolio_cycle(ranked, eur_rate)
    except portfolio.PortfolioStateCorrupted as e:
        print(f"[main] estado do portfólio corrompido, corrida abortada: {e}")
        telegram_alert.send_telegram_message(
            "⚠️ *Portfolio state corrupted*\n\n"
            f"{e}\n\nThis run was aborted on purpose (without opening/closing positions or "
            "resetting the challenge) to avoid losing history. Manual recovery is needed from "
            "the Git history (data/portfolio_state.json)."
        )
        return
    except Exception:
        print("[main] falha no ciclo do portfólio virtual:")
        traceback.print_exc()
        return

    if final_report:
        msg = telegram_alert.format_final_report(state, final_report, eur_rate)
        print("[main] relatório final do desafio:\n" + msg)
        telegram_alert.send_telegram_message(msg)
        return

    # Só envia atualização de portfólio se houver ações nesta corrida, ou pelo menos
    # uma vez a cada poucas corridas para não gerar ruído quando está tudo parado — aqui,
    # optamos por reportar sempre que há posições abertas ou ações, para visibilidade total.
    if actions or state["positions"]:
        msg = telegram_alert.format_portfolio_message(state, actions, eur_rate)
        if msg:
            print("[main] atualização de portfólio a enviar:\n" + msg)
            telegram_alert.send_telegram_message(msg)


def run_pump_watch(cex_candidates):
    """Sistema experimental de acumulação (Pump Watch, ver pump_watch.py) — independente do
    desafio de momentum. Reutiliza os candidatos CEX já descobertos nesta corrida, sem chamada
    extra de descoberta. cex_candidates pode vir vazio (falha total de descoberta nesta
    corrida) — mesmo assim as posições já abertas continuam a ser reavaliadas/fechadas."""
    if not config.PUMP_WATCH_ENABLED:
        return

    try:
        eur_rate = fx.get_usd_to_eur_rate()
        state, actions = pump_watch.run_pump_watch_cycle(cex_candidates, eur_rate)
    except pump_watch.PumpWatchStateCorrupted as e:
        print(f"[main] estado do Pump Watch corrompido, corrida abortada: {e}")
        telegram_alert.send_telegram_message(
            "⚠️ *Pump Watch state corrupted*\n\n"
            f"{e}\n\nThis run was aborted on purpose (without opening/closing positions) to "
            "avoid losing history. Manual recovery is needed from the Git history "
            "(data/pump_watch_state.json)."
        )
        return
    except Exception:
        print("[main] falha no ciclo do Pump Watch:")
        traceback.print_exc()
        return

    if actions or state["positions"]:
        msg = telegram_alert.format_pump_watch_message(state, actions, eur_rate)
        if msg:
            print("[main] atualização do Pump Watch a enviar:\n" + msg)
            telegram_alert.send_telegram_message(msg)


def main():
    print("=== Crypto Screener — início da corrida ===")

    candidates = gather_candidates()
    ranked = []
    if candidates:
        candidates = apply_security_checks(candidates)
        ranked = scoring.score_and_rank(candidates)
        print(f"[main] {len(ranked)} candidatos acima do limiar de score ({config.MIN_SCORE_TO_ALERT})")
    else:
        print("[main] nenhum candidato bruto obtido em nenhuma fonte nesta corrida.")

    run_screener_alerts(ranked)
    run_portfolio_challenge(ranked)
    run_pump_watch([c for c in candidates if c.get("tier") == "cex_small_cap"])

    print("=== Crypto Screener — fim da corrida ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
