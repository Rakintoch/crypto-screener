"""
Regista "vitórias" — posições fechadas com lucro — para começar a identificar um
"modus operandi": padrões comuns às operações que correram bem, para complementar as
"lições" tiradas das perdas (ver screener/lessons.py).

Tal como as lições, isto não altera o comportamento do bot sozinho (não ajusta
config.py automaticamente) — é memória estruturada, para consulta (via /vitorias e
/modus no Telegram, ou lendo data/wins.json diretamente) e para informar decisões
futuras sobre os critérios do screener.
"""
import json
import os

from . import config


def _load():
    # lê config.WINS_FILE em cada chamada (em vez de guardar num módulo-level constant) para
    # que os testes offline possam apontar para um ficheiro temporário sem tocar no repositório
    if not os.path.exists(config.WINS_FILE):
        return []
    try:
        with open(config.WINS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _save(wins_list):
    os.makedirs(os.path.dirname(config.WINS_FILE), exist_ok=True)
    with open(config.WINS_FILE, "w", encoding="utf-8") as f:
        json.dump(wins_list, f, indent=2, ensure_ascii=False)


def _classify(trade):
    reason = trade.get("exit_reason", "") or ""
    if "trailing stop" in reason:
        return "trailing stop caught extra upside after the target"
    if "take-profit" in reason:
        return "direct take-profit"
    return "other (manual/forced exit with profit)"


def _build_note(trade, categoria, held_minutes):
    parts = [
        f"{trade['symbol']} ({trade['tier']}) gained {trade['pnl_pct']:+.1f}% in {held_minutes:.0f} min "
        f"— {categoria}."
    ]

    liq = trade.get("entry_liquidity_usd")
    if liq is not None:
        parts.append(f"Entry liquidity: ${liq:,.0f}.")

    score = trade.get("entry_score")
    if score is not None:
        parts.append(f"Entry score: {score:.0f}.")

    sec = trade.get("entry_security_notes")
    if sec:
        parts.append(f"Entry security: {sec}.")

    return " ".join(parts)


def record_if_win(trade):
    """Chamado sempre que uma posição fecha (ver portfolio._close_position). Só regista se o
    resultado foi lucro. Devolve a entrada registada, ou None se não havia vitória a registar."""
    if trade.get("pnl_pct", 0) < 0:
        return None

    entry_ts = trade.get("entry_ts")
    exit_ts = trade.get("exit_ts")
    held_minutes = (exit_ts - entry_ts) / 60 if entry_ts and exit_ts else 0

    categoria = _classify(trade)

    entry = {
        "closed_ts": exit_ts,
        "symbol": trade.get("symbol"),
        "tier": trade.get("tier"),
        "network": trade.get("network"),
        "url": trade.get("url"),
        "held_minutes": round(held_minutes, 1),
        "exit_reason": trade.get("exit_reason"),
        "pnl_pct": round(trade.get("pnl_pct", 0), 2),
        "pnl_eur": round(trade.get("pnl_eur", 0), 2),
        "entry_score": trade.get("entry_score"),
        "entry_liquidity_usd": trade.get("entry_liquidity_usd"),
        "entry_volume_24h_usd": trade.get("entry_volume_24h_usd"),
        "entry_security_notes": trade.get("entry_security_notes"),
        # Sinais brutos na entrada (adicionado 2026-09-10) — ver a mesma nota em lessons.py;
        # guardar isto também nas vitórias é o que permite comparar sinal a sinal, não só
        # score final vs score final, entre o que correu bem e o que correu mal.
        "entry_chg_1h": trade.get("entry_chg_1h"),
        "entry_chg_24h": trade.get("entry_chg_24h"),
        "entry_chg_7d": trade.get("entry_chg_7d"),
        "entry_chg_6h": trade.get("entry_chg_6h"),
        "entry_turnover": trade.get("entry_turnover"),
        "entry_vol_liq_ratio": trade.get("entry_vol_liq_ratio"),
        "entry_boosted": trade.get("entry_boosted"),
        # Adicionado 2026-09-12 junto com o sinal de base_breakout (ver scoring.py/config.py)
        # — mesma lógica dos restantes "entry_*": guardar o sinal bruto, não só o score final.
        "entry_pool_age_minutes": trade.get("entry_pool_age_minutes"),
        "entry_base_breakout": trade.get("entry_base_breakout"),
        "categoria": categoria,
    }
    entry["nota"] = _build_note(trade, categoria, held_minutes)

    wins_list = _load()
    wins_list.append(entry)
    _save(wins_list)
    return entry


def format_wins_message(limit=5):
    """Mensagem Telegram com as vitórias mais recentes (usada pelo comando /vitorias)."""
    wins_list = _load()
    if not wins_list:
        return "🏆 No wins recorded yet — no position has closed at a profit so far."

    recent = wins_list[-limit:][::-1]
    lines = [f"🏆 *Accumulated wins* ({len(wins_list)} total, last {len(recent)}):\n"]
    for entry in recent:
        lines.append(f"• *{entry['symbol']}*: {entry['nota']}")
    return "\n".join(lines)


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def build_modus_operandi():
    """Sintetiza um "modus operandi" comparando padrões comuns às vitórias com os das lições
    (perdas, ver screener/lessons.py), para ajudar a perceber o que distingue as operações que
    resultam das que não resultam. Só compara os campos onde já há dados suficientes — com
    poucas amostras, isto é indicativo, não estatisticamente robusto."""
    from . import lessons  # import tardio para evitar import circular entre os dois módulos

    wins_list = _load()
    lessons_list = lessons._load()

    if not wins_list:
        return (
            "📊 Not enough wins recorded yet to draw a \"modus operandi\" — "
            "once positions close at a profit, this message will start comparing those wins' "
            "patterns with the lessons already recorded."
        )

    by_tier = {}
    for w in wins_list:
        by_tier.setdefault(w.get("tier"), []).append(w)
    tier_summary = ", ".join(f"{t}: {len(ws)}" for t, ws in by_tier.items()) or "n/a"

    take_profit_direto = sum(1 for w in wins_list if w.get("categoria") == "direct take-profit")
    trailing_extra = sum(
        1 for w in wins_list if w.get("categoria") == "trailing stop caught extra upside after the target"
    )

    win_held = _avg([w.get("held_minutes") for w in wins_list])
    win_scores = _avg([w.get("entry_score") for w in wins_list])
    loss_scores = _avg([l.get("entry_score") for l in lessons_list])
    win_liq = _avg([w.get("entry_liquidity_usd") for w in wins_list])
    loss_liq = _avg([l.get("entry_liquidity_usd") for l in lessons_list])

    lines = [
        f"📊 *Modus operandi* (based on {len(wins_list)} win(s) and {len(lessons_list)} lesson(s) recorded):\n",
        f"• Wins by tier: {tier_summary}",
        f"• Exit type: {take_profit_direto} direct take-profit, {trailing_extra} with extra trailing-stop gain",
    ]
    if win_held is not None:
        lines.append(f"• Average time to a profitable exit: {win_held:.0f} min")
    if win_scores is not None and loss_scores is not None:
        lines.append(
            f"• Average entry score — wins: {win_scores:.0f} vs. loss positions (lessons): {loss_scores:.0f}"
        )
    elif win_scores is not None:
        lines.append(f"• Average entry score on wins: {win_scores:.0f}")
    if win_liq is not None and loss_liq is not None:
        lines.append(
            f"• Average entry liquidity — wins: ${win_liq:,.0f} vs. loss positions (lessons): ${loss_liq:,.0f}"
        )
    elif win_liq is not None:
        lines.append(f"• Average entry liquidity on wins: ${win_liq:,.0f}")

    lines.append(
        "\nNote: with few samples these numbers aren't statistically robust yet — "
        "this summary becomes more valuable as more positions close."
    )
    return "\n".join(lines)
