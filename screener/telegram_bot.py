"""
Bot de comandos: permite pedir o estado do desafio ao bot do Telegram a qualquer
momento (comando /status), sem esperar pela próxima corrida agendada do screener
principal. Só lê os dados já guardados no repositório (não volta a consultar as
APIs de mercado), por isso é rápido e não consome quota das APIs externas.

Corre num workflow GitHub Actions separado (bot_listener.yml), que verifica
periodicamente se há mensagens novas no bot.
"""
import json
import os

import requests

from . import config
from . import lessons
from . import playbook
from . import portfolio
from . import telegram_alert

OFFSET_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot_offset.json"
)
GET_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


def _load_offset():
    if not os.path.exists(OFFSET_FILE):
        return 0
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("offset", 0)
    except Exception:
        return 0


def _save_offset(offset):
    os.makedirs(os.path.dirname(OFFSET_FILE), exist_ok=True)
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)


def _fetch_updates(offset):
    if not config.TELEGRAM_BOT_TOKEN:
        return []
    try:
        resp = requests.get(
            GET_UPDATES_URL.format(token=config.TELEGRAM_BOT_TOKEN),
            params={"offset": offset, "timeout": 0},
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:  # noqa: BLE001
        print(f"[telegram_bot] falha a obter updates: {e}")
        return []


def _handle_status_command():
    state = portfolio.load_portfolio()
    msg = telegram_alert.format_portfolio_message(state, [])
    if msg is None:
        msg = "🤖 O desafio de portfólio virtual ainda não começou (ainda não houve nenhuma compra)."
    return msg


def _handle_lessons_command():
    return lessons.format_lessons_message()


def _handle_wins_command():
    return playbook.format_wins_message()


def _handle_modus_command():
    return playbook.build_modus_operandi()


def _handle_help_command():
    return (
        "🤖 *Comandos disponíveis:*\n"
        "/status — vê o estado atual do desafio de portfólio virtual\n"
        "/licoes — vê as lições acumuladas sobre posições que fecharam com prejuízo\n"
        "/vitorias — vê as vitórias acumuladas sobre posições que fecharam com lucro\n"
        "/modus — vê o \"modus operandi\" (padrões comuns às vitórias vs. lições)\n"
        "/help — mostra esta mensagem"
    )


def process_commands():
    """Vai buscar mensagens novas ao Telegram e responde a comandos reconhecidos."""
    offset = _load_offset()
    updates = _fetch_updates(offset)

    if not updates:
        print("[telegram_bot] sem mensagens novas.")
        return

    max_update_id = offset - 1
    for update in updates:
        max_update_id = max(max_update_id, update.get("update_id", 0))
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip().lower()

        # só responde ao chat configurado — ignora mensagens de qualquer outro chat
        if chat_id != str(config.TELEGRAM_CHAT_ID):
            continue

        if text in ("/status", "/estado"):
            reply = _handle_status_command()
        elif text in ("/licoes", "/lições", "/lessons"):
            reply = _handle_lessons_command()
        elif text in ("/vitorias", "/vitórias", "/wins"):
            reply = _handle_wins_command()
        elif text in ("/modus", "/operandi", "/modusoperandi"):
            reply = _handle_modus_command()
        elif text in ("/help", "/ajuda", "/start"):
            reply = _handle_help_command()
        else:
            continue

        print(f"[telegram_bot] a responder a '{text}':\n{reply}")
        telegram_alert.send_telegram_message(reply)

    _save_offset(max_update_id + 1)


if __name__ == "__main__":
    process_commands()
