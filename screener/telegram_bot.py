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

from . import changelog
from . import config
from . import lessons
from . import playbook
from . import portfolio
from . import sources_coingecko
from . import sources_dexscreener
from . import state as state_mod
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
    try:
        state = portfolio.load_portfolio()
    except portfolio.PortfolioStateCorrupted as e:
        return f"⚠️ Não consigo mostrar o estado — {e}"
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


def _handle_changelog_command():
    return changelog.format_recent_changes()


def _find_tracked_ids_for_symbol(symbol):
    """Procura (tier, id) para um símbolo, tanto nos candidatos vistos recentemente
    (data/state.json, anti-spam de alertas) como nas posições abertas/fechadas do desafio —
    para que /preco funcione tanto para algo que acabou de aparecer num alerta como para uma
    posição já comprada."""
    seen = set()
    matches = []

    state = state_mod.load_state()
    for key, v in state.items():
        if (v.get("symbol") or "").upper() == symbol and key not in seen:
            tier, _, id_ = key.partition(":")
            matches.append((tier, id_))
            seen.add(key)

    try:
        pf = portfolio.load_portfolio()
    except portfolio.PortfolioStateCorrupted:
        pf = None
    if pf:
        for pos_key, pos in pf.get("positions", {}).items():
            if (pos.get("symbol") or "").upper() == symbol and pos_key not in seen:
                tier, _, id_ = pos_key.partition(":")
                matches.append((tier, id_))
                seen.add(pos_key)

    return matches


def _format_live_quote(tier, id_, c):
    if not c:
        return f"• `{id_}` ({tier}): não consegui obter dados de mercado agora"

    name = c.get("name") or c.get("symbol") or id_
    price = c.get("price_usd")
    price_str = f"${price:.6g}" if price is not None else "sem preço"
    chg_1h = c.get("chg_1h")
    chg_24h = c.get("chg_24h")
    parts = [f"• *{name}* ({tier})\n  💲 {price_str}"]
    if chg_1h is not None:
        parts.append(f"1h {chg_1h:+.1f}%")
    if chg_24h is not None:
        parts.append(f"24h {chg_24h:+.1f}%")
    return " | ".join(parts) if len(parts) == 1 else parts[0] + " | " + " | ".join(parts[1:])


def _handle_preco_command(arg):
    """Cotação AGORA (chamada em tempo real às APIs, não o valor guardado da última corrida)
    para um símbolo — pedido do Ricardo 2026-08-31: seguir uma moeda encontrada pelo screener
    sem esperar pelo relatório de 2h em 2h, cujos valores já estarão desatualizados."""
    symbol = (arg or "").strip().upper()
    if not symbol:
        return "Uso: /preco SÍMBOLO (ex: /preco ZORA) — mostra a cotação atual, pedida agora às APIs."

    matches = _find_tracked_ids_for_symbol(symbol)
    if not matches:
        return (
            f"🔍 Não encontrei {symbol} nos candidatos alertados recentemente nem nas posições "
            "do desafio. Só consigo consultar em tempo real algo que o screener já tenha visto "
            "pelo menos uma vez (não faço uma pesquisa livre por qualquer token)."
        )

    lines = [f"🔎 *Cotação agora — {symbol}*\n"]
    for tier, id_ in matches[:5]:
        if tier == "cex_small_cap":
            fresh = sources_coingecko.fetch_by_ids([id_])
            c = fresh.get(id_)
        else:
            fresh = sources_dexscreener.fetch_market_data_for_addresses([id_])
            c = fresh[0] if fresh else None
        lines.append(_format_live_quote(tier, id_, c))
    return "\n".join(lines)


def _handle_help_command():
    return (
        "🤖 *Comandos disponíveis:*\n"
        "/status — vê o estado atual do desafio de portfólio virtual\n"
        "/preco SÍMBOLO — cotação AGORA (tempo real) de um token já visto pelo screener\n"
        "/licoes — vê as lições acumuladas sobre posições que fecharam com prejuízo\n"
        "/vitorias — vê as vitórias acumuladas sobre posições que fecharam com lucro\n"
        "/modus — vê o \"modus operandi\" (padrões comuns às vitórias vs. lições)\n"
        "/mudancas — vê o histórico de melhorias implementadas na estratégia/config do bot\n"
        "/help — mostra esta mensagem"
    )


def _send_pending_changelog_announcements():
    """Anuncia no Telegram qualquer mudança de estratégia registada por uma autoanálise que
    ainda não tenha sido comunicada — corre em toda a execução do bot_listener.yml (a cada
    15 min), independentemente de teres enviado algum comando. Assim, uma melhoria fica
    anunciada perto do momento em que é implementada, sem seres tu a ter de perguntar."""
    pending = changelog.pending_announcements()
    if not pending:
        return

    for entry in pending:
        telegram_alert.send_telegram_message(changelog.format_announcement(entry))

    changelog.mark_announced([e["ts"] for e in pending])


def process_commands():
    """Vai buscar mensagens novas ao Telegram, responde a comandos reconhecidos, e anuncia
    quaisquer mudanças de estratégia pendentes (ver _send_pending_changelog_announcements)."""
    _send_pending_changelog_announcements()

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
        elif text.startswith("/preco") or text.startswith("/preço") or text.startswith("/live"):
            _, _, arg = text.partition(" ")
            reply = _handle_preco_command(arg)
        elif text in ("/licoes", "/lições", "/lessons"):
            reply = _handle_lessons_command()
        elif text in ("/vitorias", "/vitórias", "/wins"):
            reply = _handle_wins_command()
        elif text in ("/modus", "/operandi", "/modusoperandi"):
            reply = _handle_modus_command()
        elif text in ("/mudancas", "/mudanças", "/melhorias", "/changelog"):
            reply = _handle_changelog_command()
        elif text in ("/help", "/ajuda", "/start"):
            reply = _handle_help_command()
        else:
            continue

        print(f"[telegram_bot] a responder a '{text}':\n{reply}")
        telegram_alert.send_telegram_message(reply)

    _save_offset(max_update_id + 1)


if __name__ == "__main__":
    process_commands()
