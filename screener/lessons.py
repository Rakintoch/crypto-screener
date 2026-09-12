"""
Regista "lições" sobre posições fechadas com resultado pior do que o esperado (prejuízo),
para ir construindo ao longo do tempo um histórico de padrões — tendo sempre como referência
os critérios de entrada definidos em config.py (score mínimo, liquidez mínima, idade da pool,
segurança GoPlus, take-profit/stop-loss).

Isto não altera o comportamento do bot sozinho (não ajusta config.py automaticamente) — é só
memória estruturada, para consulta (via /licoes no Telegram, ou lendo data/lessons.json
diretamente) e para informar decisões futuras sobre os critérios do screener.
"""
import json
import os

from . import config


def _load():
    # lê config.LESSONS_FILE em cada chamada (em vez de guardar num módulo-level constant) para
    # que os testes offline possam apontar para um ficheiro temporário sem tocar no repositório
    if not os.path.exists(config.LESSONS_FILE):
        return []
    try:
        with open(config.LESSONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _save(lessons_list):
    os.makedirs(os.path.dirname(config.LESSONS_FILE), exist_ok=True)
    with open(config.LESSONS_FILE, "w", encoding="utf-8") as f:
        json.dump(lessons_list, f, indent=2, ensure_ascii=False)


def _classify(trade, held_minutes):
    reason = trade.get("exit_reason", "") or ""
    pnl_pct = trade.get("pnl_pct", 0)

    if pnl_pct <= -80:
        categoria = "colapso quase total (possível rug/dump)"
    elif "sem dados de mercado" in reason:
        categoria = "perda de liquidez/dados (possível rug)"
    elif "score caiu" in reason or "invalidada" in reason:
        categoria = "tese de momentum invalidada (score caiu)"
    elif "stop-loss" in reason:
        categoria = "stop-loss atingido"
    else:
        categoria = "outro"

    if held_minutes < 60:
        velocidade = "muito rápida (<1h)"
    elif held_minutes < 360:
        velocidade = "rápida (1-6h)"
    else:
        velocidade = "lenta (>6h)"

    return categoria, velocidade


def _build_note(trade, categoria, velocidade, held_minutes):
    parts = [
        f"{trade['symbol']} ({trade['tier']}) perdeu {trade['pnl_pct']:+.1f}% em {held_minutes:.0f} min "
        f"({velocidade}) — {categoria}."
    ]

    liq = trade.get("entry_liquidity_usd")
    if liq is not None:
        parts.append(f"Liquidez na entrada: ${liq:,.0f}.")

    score = trade.get("entry_score")
    if score is not None:
        parts.append(f"Score na entrada: {score:.0f} (mínimo exigido para comprar: {config.ENTRY_MIN_SCORE}).")

    sec = trade.get("entry_security_notes")
    if sec:
        parts.append(f"Segurança na entrada: {sec}.")

    if categoria.startswith("colapso") and velocidade.startswith("muito rápida"):
        parts.append(
            "Nota: mesmo passando todos os filtros atuais (liquidez mínima, idade mínima da pool, "
            "verificação de segurança), este tipo de colapso muito rápido é difícil de evitar só com "
            "verificações periódicas — vale a pena rever se os limiares de config.py (ex: "
            "DEX_MIN_LIQUIDITY_USD, DEX_MIN_POOL_AGE_MINUTES) precisam de ser mais apertados para "
            "tokens tão recentes."
        )
    elif categoria.startswith("colapso") and velocidade.startswith("lenta"):
        parts.append(
            "Nota: a queda foi lenta o suficiente para, em teoria, ter sido apanhada bem mais cedo por "
            "uma verificação frequente — se isto se repetir, confirmar que o monitor de 15 min está "
            "mesmo a correr sem falhas nesse intervalo."
        )

    return " ".join(parts)


def record_if_lesson(trade):
    """Chamado sempre que uma posição fecha (ver portfolio._close_position). Só regista se o
    resultado foi um prejuízo. Devolve a entrada registada, ou None se não havia lição a tirar."""
    if trade.get("pnl_pct", 0) >= 0:
        return None

    entry_ts = trade.get("entry_ts")
    exit_ts = trade.get("exit_ts")
    held_minutes = (exit_ts - entry_ts) / 60 if entry_ts and exit_ts else 0

    categoria, velocidade = _classify(trade, held_minutes)

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
        # Sinais brutos na entrada (adicionado 2026-09-10, ver autoanálise em config.py junto a
        # SELECTION_SCORE_CEILING) — permitem, numa próxima autoanálise, decompor QUAL sinal
        # falhou, em vez de só conseguir provar que o score final combinado não prevê o
        # resultado. None nos campos que não se aplicam à camada desta posição.
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
        "velocidade_queda": velocidade,
    }
    entry["licao"] = _build_note(trade, categoria, velocidade, held_minutes)

    lessons_list = _load()
    lessons_list.append(entry)
    _save(lessons_list)
    return entry


def format_lessons_message(limit=5):
    """Mensagem Telegram com as lições mais recentes (usada pelo comando /licoes)."""
    lessons_list = _load()
    if not lessons_list:
        return "📚 Ainda não há lições registadas — nenhuma posição fechou com prejuízo até agora."

    recent = lessons_list[-limit:][::-1]
    lines = [f"📚 *Lições acumuladas* ({len(lessons_list)} no total, últimas {len(recent)}):\n"]
    for entry in recent:
        lines.append(f"• *{entry['symbol']}*: {entry['licao']}")
    return "\n".join(lines)
