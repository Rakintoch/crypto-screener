"""Formatação e envio de alertas para o Telegram."""
import time

import requests

from . import config

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

DISCLAIMER = (
    "\n\n⚠️ _Isto não é aconselhamento financeiro. É um ranking automático de momentum, "
    "não uma previsão. Micro-caps e tokens DEX têm risco real de perda total (incluindo rugs "
    "e honeypots não detetados). Faz sempre a tua própria pesquisa antes de agires._"
)


def _fmt_pct(v):
    if v is None:
        return "n/d"
    return f"{v:+.1f}%"


def _fmt_usd(v):
    if v is None:
        return "n/d"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    if v >= 0.01 or v == 0:
        return f"${v:.2f}"
    # tokens micro-cap costumam ter preços com muitos zeros (ex: $0.0000123) —
    # mostra dígitos significativos em vez de arredondar tudo para $0.00
    return f"${v:.8f}".rstrip("0").rstrip(".")


def format_candidate_line(c, rank):
    security = c.get("security") or {}
    sec_tag = "✅" if security.get("checked") and security.get("safe") else ("⚠️" if security.get("checked") else "❔")

    if c["tier"] == "cex_small_cap":
        return (
            f"{rank}. *{c['symbol']}* ({c['name']}) — score {c['score']}\n"
            f"   💰 {_fmt_usd(c['price_usd'])} | MCap {_fmt_usd(c['market_cap'])} (#{c.get('market_cap_rank', '?')})\n"
            f"   📈 1h {_fmt_pct(c.get('chg_1h'))} | 24h {_fmt_pct(c.get('chg_24h'))} | 7d {_fmt_pct(c.get('chg_7d'))}\n"
            f"   🔊 Vol 24h {_fmt_usd(c['volume_24h'])} (turnover {c['turnover']:.0%})\n"
            f"   🔗 {c['url']}"
        )

    boosted_tag = " 🚀boosted" if c.get("boosted") else ""
    return (
        f"{rank}. {sec_tag} *{c['symbol']}* [{c.get('network')}] — score {c['score']}{boosted_tag}\n"
        f"   💰 {_fmt_usd(c['price_usd'])} | FDV {_fmt_usd(c['market_cap'])}\n"
        f"   📈 1h {_fmt_pct(c.get('chg_1h'))} | 6h {_fmt_pct(c.get('chg_6h'))}\n"
        f"   💧 Liq {_fmt_usd(c['liquidity_usd'])} | Vol 24h {_fmt_usd(c['volume_24h'])}\n"
        f"   🔒 Segurança: {security.get('notes', 'n/d')}\n"
        f"   🔗 {c.get('url', 'n/d')}"
    )


def build_message(cex_top, dex_top):
    lines = ["🔎 *Crypto Screener — novos sinais de momentum*\n"]

    if cex_top:
        lines.append("🐢 *Small-cap estabelecida (CEX)*")
        for i, c in enumerate(cex_top, 1):
            lines.append(format_candidate_line(c, i))
        lines.append("")

    if dex_top:
        lines.append("🚀 *Micro-cap agressiva (DEX)*")
        for i, c in enumerate(dex_top, 1):
            lines.append(format_candidate_line(c, i))

    if not cex_top and not dex_top:
        lines.append("_Sem candidatos acima do limiar nesta corrida._")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _fmt_eur(v):
    return f"€{v:,.2f}".replace(",", " ")


def format_portfolio_message(state, actions):
    """Mensagem de estado do desafio de portfólio virtual (100% simulado, dados reais)."""
    if state["status"] == "not_started":
        return None  # ainda não há nada para reportar

    lines = ["💼 *Desafio Portfólio Virtual (100% simulado, dados reais)*"]

    if state["status"] == "active":
        days_elapsed = (time.time() - state["start_ts"]) / 86400
        days_total = config.CHALLENGE_DURATION_DAYS
        lines.append(f"📅 Dia {days_elapsed:.1f} / {days_total}")
    elif state["status"] == "finished":
        lines.append("🏁 *Desafio concluído.*")

    open_value = sum(p["qty"] * p.get("last_price_eur", p["entry_price_eur"]) for p in state["positions"].values())
    equity = state["cash_eur"] + open_value
    pnl = equity - state["starting_balance_eur"]
    pnl_pct = pnl / state["starting_balance_eur"] * 100

    lines.append(f"💰 *Saldo Total: {_fmt_eur(equity)}* ({pnl:+.2f} EUR, {pnl_pct:+.1f}% desde o início)")
    lines.append(
        f"   ↳ Cash livre: {_fmt_eur(state['cash_eur'])} + "
        f"Posições abertas ({len(state['positions'])}): {_fmt_eur(open_value)}"
    )
    if state.get("capital_protection_active"):
        lines.append("   🛑 Proteção de capital ativa — sem novas entradas até ao fim do desafio")

    for key, pos in state["positions"].items():
        chg = (pos.get("last_price_eur", pos["entry_price_eur"]) / pos["entry_price_eur"] - 1) * 100
        lines.append(f"   • {pos['symbol']} ({pos['tier']}): {chg:+.1f}% desde a entrada")

    buys = [a for a in actions if a["action"] == "buy"]
    sells = [a for a in actions if a["action"] == "sell"]

    if buys:
        lines.append("\n🟢 *Compras nesta corrida:*")
        for a in buys:
            lines.append(f"   {a['symbol']}: {_fmt_eur(a['cost_eur'])} a {a['entry_price_eur']:.6f} EUR/unid.")

    if sells:
        lines.append("\n🔴 *Vendas nesta corrida:*")
        for a in sells:
            lines.append(f"   {a['symbol']}: {_fmt_eur(a['proceeds_eur'])} ({a['pnl_pct']:+.1f}%) — {a['exit_reason']}")

    return "\n".join(lines)


def format_final_report(state, report):
    lines = [
        "🏁 *FIM DO DESAFIO — resultado dos 10 dias*",
        "",
        f"💶 Saldo inicial: {_fmt_eur(report['starting_balance_eur'])}",
        f"💶 Saldo final: {_fmt_eur(report['final_balance_eur'])}",
        f"📊 Resultado: {report['pnl_eur']:+.2f} EUR ({report['pnl_pct']:+.1f}%)",
        f"🔁 Total de trades fechados: {report['num_trades']}",
        "",
        "*Histórico de trades:*",
    ]
    for t in state["closed_trades"]:
        lines.append(f"• {t['symbol']} ({t['tier']}): {t['pnl_pct']:+.1f}% — {t['exit_reason']}")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def send_telegram_message(text):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[telegram_alert] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID não configurados — a saltar envio.")
        print(text)
        return False

    # Telegram limita mensagens a 4096 caracteres — parte em blocos se necessário
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]

    ok = True
    for chunk in chunks:
        try:
            resp = requests.post(
                TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN),
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                print(f"[telegram_alert] erro Telegram {resp.status_code}: {resp.text}")
                ok = False
        except Exception as e:  # noqa: BLE001
            print(f"[telegram_alert] exceção a enviar para Telegram: {e}")
            ok = False

    return ok
