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


def _fmt_venue(v):
    """Nota da venue mais líquida usada como referência do preço de entrada — pedido do
    Ricardo 2026-09-14 (caso SOXSB): o preço guardado é uma média do CoinGecko entre várias
    venues, mas uma compra real só pode ser executada numa de cada vez (ver
    sources_coingecko.fetch_top_venue). Vazio quando não há venue guardada (posições
    dex_micro_cap, que já vêm de uma pool única, ou se a chamada extra tiver falhado)."""
    return f", via {v}" if v else ""


def _fmt_catalyst(tags):
    """Etiquetas de catalisador fundamental na compra (2026-09-25, ver catalysts.py)."""
    if not tags:
        return ""
    return " 📰 " + ", ".join(t.replace("_", " ") for t in tags)


def _fmt_contract(address):
    """Endereço do contrato a mostrar na listagem de posições abertas — pedido do Ricardo
    2026-09-14. O chamador já resolve o endereço a passar: o próprio "id" da posição para
    dex_micro_cap (já é o token/pool on-chain, sem chamada extra), ou
    entry_contract_address para cex_small_cap, guardado no momento da compra via
    sources_coingecko.fetch_contract_address. Vazio quando não há endereço (moeda nativa de
    uma chain própria, ex: ARDR, ou se a chamada extra tiver falhado)."""
    return f" | 📝 {address}" if address else ""


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
            f"   💰 MCap {_fmt_usd(c['market_cap'])} (#{c.get('market_cap_rank', '?')})\n"
            f"   📈 1h {_fmt_pct(c.get('chg_1h'))} | 24h {_fmt_pct(c.get('chg_24h'))} | 7d {_fmt_pct(c.get('chg_7d'))}\n"
            f"   🔊 Vol 24h {_fmt_usd(c['volume_24h'])} (turnover {c['turnover']:.0%})\n"
            f"   🔗 {c['url']}"
        )

    boosted_tag = " 🚀boosted" if c.get("boosted") else ""
    return (
        f"{rank}. {sec_tag} *{c['symbol']}* [{c.get('network')}] — score {c['score']}{boosted_tag}\n"
        f"   💰 FDV {_fmt_usd(c['market_cap'])}\n"
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
        # Ciclos contínuos (2026-09-24): o dia conta desde o início do ciclo ATUAL
        cycle_start = state.get("cycle_start_ts") or state["start_ts"]
        days_elapsed = (time.time() - cycle_start) / 86400
        days_total = config.CHALLENGE_DURATION_DAYS
        cycle_part = f"Cycle {state['cycle_number']} · " if state.get("cycle_number") else ""
        lines.append(f"📅 {cycle_part}Day {days_elapsed:.1f} / {days_total}")
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
        lines.append("   🛑 Capital protection active — no new entries until the next cycle")

    for key, pos in state["positions"].items():
        chg = (pos.get("last_price_eur", pos["entry_price_eur"]) / pos["entry_price_eur"] - 1) * 100
        mcap = pos.get("last_market_cap")
        mc_part = f" | MC {_fmt_usd(mcap)}" if mcap else ""
        # dex_micro_cap já vem de UMA pool/token on-chain — o próprio "id" da posição já É
        # o endereço do contrato, sem precisar de guardar um campo extra (ver portfolio.py).
        contract_addr = pos["id"] if pos["tier"] == "dex_micro_cap" else pos.get("entry_contract_address")
        lines.append(f"   • {pos['symbol']} ({pos['tier']}): {chg:+.1f}% since entry{mc_part}{_fmt_contract(contract_addr)}")

    buys = [a for a in actions if a["action"] == "buy"]
    sells = [a for a in actions if a["action"] == "sell"]

    if buys:
        lines.append("\n🟢 *Buys this run:*")
        for a in buys:
            lines.append(
                f"   {a['symbol']}: {_fmt_usd_amount(a['cost_eur'] / rate)} "
                f"(MC {_fmt_usd(a.get('entry_market_cap'))}{_fmt_venue(a.get('entry_venue'))})"
                f"{_fmt_catalyst(a.get('entry_catalyst_tags'))}"
            )

    if sells:
        lines.append("\n🔴 *Sells this run:*")
        for a in sells:
            lines.append(
                f"   {a['symbol']}: {_fmt_usd_amount(a['proceeds_eur'] / rate)} "
                f"({a['pnl_pct']:+.1f}%) — {a['exit_reason']}"
            )

    return "\n".join(lines)


def format_cycle_checkpoint(state, report, eur_rate=None):
    """Fim de um ciclo em modo contínuo (config.CONTINUOUS_CYCLES, pedido do Ricardo
    2026-09-24): resumo do ciclo, sem liquidação nem reset — o ciclo seguinte arranca logo."""
    rate = _eur_rate_for(state, eur_rate)
    carried = report.get("open_positions_carried") or []
    lines = [
        f"🔄 *CYCLE {report['challenge_number']} CLOSED — {config.CHALLENGE_DURATION_DAYS}-day review checkpoint*",
        "",
        f"💵 Cycle start: {_fmt_usd_amount(report['starting_balance_eur'] / rate)}",
        f"💵 Cycle end: {_fmt_usd_amount(report['final_balance_eur'] / rate)}",
        f"📊 Cycle result: {report['pnl_eur'] / rate:+.2f} USD ({report['pnl_pct']:+.1f}%)",
        f"📈 Since inception: {report['inception_pnl_pct']:+.1f}%",
        f"🔁 Closed trades this cycle: {report['num_trades']} (win rate {report['win_rate_pct']:.0f}%)",
        f"📂 Positions carried over: {', '.join(carried) if carried else 'none'}",
        "",
        f"No liquidation, no reset — cycle {report['challenge_number'] + 1} starts now with the same "
        "balance and positions. Lessons from this cycle feed the next strategy review.",
    ]
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_final_report(state, report, eur_rate=None):
    if report.get("type") == "cycle_checkpoint":
        return format_cycle_checkpoint(state, report, eur_rate)
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


def format_pump_watch_review(state, review, eur_rate=None):
    """Fecho de um ciclo de revisão do Pump Watch (pedido do Ricardo 2026-09-24) — o sistema
    continua com o mesmo saldo e posições; o resumo alimenta a revisão diária da estratégia."""
    rate = _eur_rate_for(state, eur_rate)
    carried = review.get("open_positions_carried") or []
    trigger = (f"{config.PUMP_WATCH_REVIEW_AFTER_TRADES} closed trades" if review["trigger"] == "trades"
               else f"{config.PUMP_WATCH_REVIEW_AFTER_DAYS} days")
    lines = [
        f"🔬 *Pump Watch — review cycle {review['number']} closed* ({trigger})",
        "",
        f"🔁 Closed trades: {review['num_trades']} (win rate {review['win_rate_pct']:.0f}%, "
        f"avg {review['avg_pnl_pct']:+.1f}%)",
        f"💵 Total: {_fmt_usd_amount(review['start_total_eur'] / rate)} → {_fmt_usd_amount(review['end_total_eur'] / rate)}",
        f"📂 Positions carried over: {', '.join(carried) if carried else 'none'}",
        "",
        f"No reset — review cycle {review['number'] + 1} starts now. The strategy review uses this "
        "data to decide whether the accumulation thresholds should change.",
    ]
    return "\n".join(lines)


def format_pump_watch_message(state, actions, eur_rate=None):
    """Mensagem do sistema experimental 'Pump Watch' — sinal de acumulação (ver pump_watch.py),
    totalmente independente do desafio de portfólio principal (pedido do Ricardo 2026-09-14).
    Enviada como mensagem própria, separada da do desafio. Só devolve algo quando há uma
    posição aberta ou uma ação (compra/venda) a reportar nesta corrida."""
    if state["status"] == "not_started" and not actions:
        return None

    rate = _eur_rate_for(state, eur_rate)

    lines = ["🎯 *Pump Watch (accumulation signal — experimental)*"]

    open_value = sum(p["qty"] * p.get("last_price_eur", p["entry_price_eur"]) for p in state["positions"].values())
    total = state["cash_eur"] + state["reserve_eur"] + open_value
    pnl = total - state["starting_balance_eur"]
    pnl_pct = (pnl / state["starting_balance_eur"] * 100) if state["starting_balance_eur"] else 0

    lines.append(
        f"💰 *Total: {_fmt_usd_amount(total / rate)}* "
        f"({pnl / rate:+.2f} USD, {pnl_pct:+.1f}% since inception)"
    )
    lines.append(
        f"   ↳ Tradable cash: {_fmt_usd_amount(state['cash_eur'] / rate)} + "
        f"Reserved profit: {_fmt_usd_amount(state['reserve_eur'] / rate)} + "
        f"Open ({len(state['positions'])}/{config.PUMP_WATCH_MAX_POSITIONS}): {_fmt_usd_amount(open_value / rate)}"
    )

    for pos in state["positions"].values():
        chg = (pos.get("last_price_eur", pos["entry_price_eur"]) / pos["entry_price_eur"] - 1) * 100
        peak_chg = (pos.get("peak_price_eur", pos["entry_price_eur"]) / pos["entry_price_eur"] - 1) * 100
        mcap = pos.get("last_market_cap") or pos.get("entry_market_cap")
        mc_part = f" | MC {_fmt_usd(mcap)}" if mcap else ""
        # Pump Watch é sempre cex_small_cap (ver docstring de pump_watch.py) — o endereço,
        # quando existe, vem sempre de entry_contract_address.
        lines.append(
            f"   • {pos['symbol']}: {chg:+.1f}% since entry (peak {peak_chg:+.1f}%){mc_part}"
            f"{_fmt_contract(pos.get('entry_contract_address'))}"
        )

    buys = [a for a in actions if a["action"] == "buy"]
    sells = [a for a in actions if a["action"] == "sell"]

    if buys:
        lines.append("\n🟢 *New entries (accumulation signal):*")
        for a in buys:
            lines.append(
                f"   {a['symbol']}: {_fmt_usd_amount(a['cost_eur'] / rate)} "
                f"(MC {_fmt_usd(a.get('entry_market_cap'))}{_fmt_venue(a.get('entry_venue'))})"
                f"{_fmt_catalyst(a.get('entry_catalyst_tags'))}"
            )

    if sells:
        lines.append("\n🔴 *Exits:*")
        for a in sells:
            lines.append(
                f"   {a['symbol']}: {_fmt_usd_amount(a['proceeds_eur'] / rate)} "
                f"({a['pnl_pct']:+.1f}%) — {a['exit_reason']}"
            )
            if a.get("reserved_eur"):
                lines.append(
                    f"      ↳ {_fmt_usd_amount(a['reserved_eur'] / rate)} of the profit moved "
                    "to reserve (not reinvested)"
                )

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
