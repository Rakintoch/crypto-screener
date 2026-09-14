"""
Monitor leve das posições já abertas no desafio de portfólio virtual.

Corre dentro do bot_listener.yml, a cada 15 minutos — muito mais frequente do que o
screener principal (a cada 2h) — mas sem repetir a parte cara da descoberta de tokens novos.
Só reavalia os preços das posições que já existem e verifica saídas (take-profit com trailing
stop, stop-loss, invalidação de score, liquidação forçada ao fim dos 10 dias). Nunca abre
posições novas — isso continua a cargo exclusivo do screener principal.

Isto existe para reduzir o intervalo entre verificações de posições abertas sem criar um
terceiro workflow (e sem gastar minutos extra do GitHub Actions): aproveita uma execução que
já ia acontecer de qualquer forma para checar comandos do Telegram.
"""
import sys
import traceback

from . import config
from . import fx
from . import portfolio
from . import pump_watch
from . import telegram_alert


def _run_portfolio_monitor():
    """Desafio de portfólio virtual principal (momentum) — ver portfolio.py."""
    try:
        state = portfolio.load_portfolio()
    except portfolio.PortfolioStateCorrupted as e:
        print(f"[position_monitor] estado do portfólio corrompido, corrida abortada: {e}")
        telegram_alert.send_telegram_message(
            "⚠️ *Portfolio state corrupted*\n\n"
            f"{e}\n\nThis run was aborted on purpose (without opening/closing positions or "
            "resetting the challenge) to avoid losing history. Manual recovery is needed from "
            "the Git history (data/portfolio_state.json)."
        )
        return

    if state["status"] != "active" or not state["positions"]:
        print("[position_monitor] nada para monitorizar (desafio não ativo ou sem posições abertas).")
        return

    try:
        eur_rate = fx.get_usd_to_eur_rate()
        state, actions, final_report = portfolio.run_exit_check_cycle(eur_rate)
    except portfolio.PortfolioStateCorrupted as e:
        print(f"[position_monitor] estado do portfólio corrompido, corrida abortada: {e}")
        telegram_alert.send_telegram_message(
            "⚠️ *Portfolio state corrupted*\n\n"
            f"{e}\n\nThis run was aborted on purpose (without opening/closing positions or "
            "resetting the challenge) to avoid losing history. Manual recovery is needed from "
            "the Git history (data/portfolio_state.json)."
        )
        return
    except Exception:
        print("[position_monitor] falha no ciclo de monitorização:")
        traceback.print_exc()
        return

    if final_report:
        msg = telegram_alert.format_final_report(state, final_report, eur_rate)
        print("[position_monitor] relatório final do desafio:\n" + msg)
        telegram_alert.send_telegram_message(msg)
        return

    if actions:
        msg = telegram_alert.format_portfolio_message(state, actions, eur_rate)
        if msg:
            print("[position_monitor] saída(s) detetada(s), a enviar atualização:\n" + msg)
            telegram_alert.send_telegram_message(msg)
    else:
        print("[position_monitor] posições reavaliadas, sem saídas nesta verificação.")


def _run_pump_watch_monitor():
    """Sistema experimental Pump Watch (acumulação, ver pump_watch.py) — completamente
    independente do desafio principal acima, por isso corre sempre, mesmo que o desafio
    principal esteja inativo/sem posições."""
    try:
        state = pump_watch.load_state()
    except pump_watch.PumpWatchStateCorrupted as e:
        print(f"[position_monitor] estado do Pump Watch corrompido, corrida abortada: {e}")
        telegram_alert.send_telegram_message(
            "⚠️ *Pump Watch state corrupted*\n\n"
            f"{e}\n\nThis run was aborted on purpose to avoid losing history. Manual recovery "
            "is needed from the Git history (data/pump_watch_state.json)."
        )
        return

    if not state["positions"]:
        print("[position_monitor] Pump Watch: sem posições abertas para monitorizar.")
        return

    try:
        eur_rate = fx.get_usd_to_eur_rate()
        state, actions = pump_watch.run_exit_check_cycle(eur_rate)
    except pump_watch.PumpWatchStateCorrupted as e:
        print(f"[position_monitor] estado do Pump Watch corrompido, corrida abortada: {e}")
        telegram_alert.send_telegram_message(
            "⚠️ *Pump Watch state corrupted*\n\n"
            f"{e}\n\nThis run was aborted on purpose to avoid losing history. Manual recovery "
            "is needed from the Git history (data/pump_watch_state.json)."
        )
        return
    except Exception:
        print("[position_monitor] falha no ciclo de monitorização do Pump Watch:")
        traceback.print_exc()
        return

    if actions:
        msg = telegram_alert.format_pump_watch_message(state, actions, eur_rate)
        if msg:
            print("[position_monitor] Pump Watch: saída(s) detetada(s), a enviar atualização:\n" + msg)
            telegram_alert.send_telegram_message(msg)
    else:
        print("[position_monitor] Pump Watch: posições reavaliadas, sem saídas nesta verificação.")


def main():
    if not config.POSITION_MONITOR_ENABLED:
        print("[position_monitor] desativado por configuração — a saltar.")
        return 0

    if config.PORTFOLIO_ENABLED:
        _run_portfolio_monitor()

    if config.PUMP_WATCH_ENABLED:
        _run_pump_watch_monitor()

    return 0


if __name__ == "__main__":
    sys.exit(main())
