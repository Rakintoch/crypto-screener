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
from . import telegram_alert


def main():
    if not config.PORTFOLIO_ENABLED or not config.POSITION_MONITOR_ENABLED:
        print("[position_monitor] desativado por configuração — a saltar.")
        return 0

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
        return 0

    if state["status"] != "active" or not state["positions"]:
        print("[position_monitor] nada para monitorizar (desafio não ativo ou sem posições abertas).")
        return 0

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
        return 0
    except Exception:
        print("[position_monitor] falha no ciclo de monitorização:")
        traceback.print_exc()
        return 0

    if final_report:
        msg = telegram_alert.format_final_report(state, final_report)
        print("[position_monitor] relatório final do desafio:\n" + msg)
        telegram_alert.send_telegram_message(msg)
        return 0

    if actions:
        msg = telegram_alert.format_portfolio_message(state, actions)
        if msg:
            print("[position_monitor] saída(s) detetada(s), a enviar atualização:\n" + msg)
            telegram_alert.send_telegram_message(msg)
    else:
        print("[position_monitor] posições reavaliadas, sem saídas nesta verificação.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
