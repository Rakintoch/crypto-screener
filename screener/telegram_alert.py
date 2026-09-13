"""Formatação e envio de alertas para o Telegram."""
import time

import requests

from . import config
from . import fx

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

DISCLAIMER = (
    "\n\n⚠️ _This is not financial advice. It's an automated momentum ranking, "
    "not a prediction. Micro-caps and DEX tokens carry real risk of total loss (including "
    "undetected rugs and honeypots). Always do your own research before acting._"
)


def _fmt_pct(v):
    if v is None:
        return "n/a"
    return f"{v:+.1f}%"


def _fmt_usd(v):
    if v is None:
        return "n/a"
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
        f"   🔒 Security: {security.get('notes', 'n/a')}\n"
        f"   🔗 {c.get('url', 'n/a')}"
    )


def build_message(cex_top, dex_top):
    lines = ["🔎 *Crypto Screener — new momentum signals*\n"]

    if cex_top:
        lines.append("🐢 *Established small-cap (CEX)*")
        for i, c in enumerate(cex_top, 1):
            lines.append(format_candidate_line(c, i))
        lines.append("")

    if dex_top:
        lines.append("🚀 *Aggressive micro-cap (DEX)*")
        for i, c in enumerate(dex_top, 1):
            lines.append(format_candidate_line(c, i))

    if not cex_top and not dex_top:
        lines.append("_No candidates above the threshold this run._")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _fmt_usd_amount(v):
    """Formata um valor monetário do desafio (saldo, custo, proventos — não um preço unitário
    de mercado) em dólares, com separador de milhares. A contabilidade interna do desafio
    continua em EUR (ver fx.py) — isto é só para a apresentação (pedido do Ricardo 2026-09-13:
    mostrar tudo em $ nas mensagens do Telegram)."""
    return f"${v:,.2f}".replace(",", " ")


def _eur_rate_for(state, eur_rate):
    """Taxa USD->EUR a usar na conversão para apresentação: a da corrida atual quando
    disponível (main.py/position_monitor.py já a têm fresca), senão a última guardada no
    estado (usada pelo /status, que não volta a chamar a API de câmbio), senão o fallback
    fixo de fx.py."""
    return eur_rate or state.get("last_eur_rate") or fx.FALLBACK_USD_TO_EUR


def format_portfolio_message(state, actions, eur_rate=None):
    """Mensagem de estado do desafio de portfólio virtual (100% simulado, dados reais).
    Todos os valores são apresentados em USD ($) — a contabilidade interna continua em EUR,
    a conversão é só para a mensagem (pedido do Ricardo 2026-09-13)."""
    if state["status"] == "not_started":
        return None  # ainda não há nada para reportar

    rate = _eur_rate_for(state, eur_rate)

    lines = ["💼 *Virtual Portfolio Challenge (100% simulated, real data)*"]

    if state["status"] == "active":
        days_elapsed = (time.time() - state["start_ts"]) / 86400
        days_total = config.CHALLENGE_DURATION_DAYS
        lines.append(f"📅 Day {days_elapsed:.1f} / {days_total}")
    elif state["status"] == "finished":
        lines.append("🏁 *Challenge complete.*")

    open_value = sum(p["qty"] * p.get("last_price_eur", p["entry_price_eur"]) for p in state["positions"].values())
    equity = state["cash_eur"] + open_value
    pnl = equity - state["starting_balance_eur"]
    pnl_pct = pnl / state["starting_balance_eur"] * 100

    lines.append(
        f"💰 *Total Balance: {_fmt_usd_amount(equity / rate)}* "
        f"({pnl / rate:+.2f} USD, {pnl_pct:+.1f}% since inception)"
    )
    lines.append(
        f"   ↳ Free cash: {_fmt_usd_amount(state['cash_eur'] / rate)} + "
        f"Open positions ({len(state['positions'])}): {_fmt_usd_amount(open_value / rate)}"
    )
    if state.get("capital_protection_active"):
        lines.append("   🛑 Capital protection active — no new entries until the challenge ends")

    for key, pos in state["positions"].items():
        chg = (pos.get("last_price_eur", pos["entry_price_eur"]) / pos["entry_price_eur"] - 1) * 100
        mcap = pos.get("last_market_cap")
        mc_part = f" | MC {_fmt_usd(mcap)}" if mcap else ""
        lines.append(f"   • {pos['symbol']} ({pos['tier']}): {chg:+.1f}% since entry{mc_part}")

    buys = [a for a in actions if a["action"] == "buy"]
    sells = [a for a in actions if a["action"] == "sell"]

    if buys:
        lines.append("\n🟢 *Buys this run:*")
        for a in buys:
            lines.append(
                f"   {a['symbol']}: {_fmt_usd_amount(a['cost_eur'] / rate)} "
                f"at ${a['entry_price_eur'] / rate:.6f}/unit."
            )

    if sells:
        lines.append("\n🔴 *Sells this run:*")
        for a in sells:
            lines.append(
                f"   {a['symbol']}: {_fmt_usd_amount(a['proceeds_eur'] / rate)} "
                f"({a['pnl_pct']:+.1f}%) — {a['exit_reason']}"
            )

    return "\n".join(lines)


def format_final_report(state, report, eur_rate=None):
    rate = _eur_rate_for(state, eur_rate)
    lines = [
        "🏁 *CHALLENGE OVER — 10-day result*",
        "",
        f"💵 Starting balance: {_fmt_usd_amount(report['starting_balance_eur'] / rate)}",
        f"💵 Final balance: {_fmt_usd_amount(report['final_balance_eur'] / rate)}",
        f"📊 Result: {report['pnl_eur'] / rate:+.2f} USD ({report['pnl_pct']:+.1f}%)",
        f"🔁 Total closed trades: {report['num_trades']}",
        "",
        "*Trade history:*",
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
