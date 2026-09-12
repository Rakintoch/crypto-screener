"""
Motor de scoring: transforma os candidatos brutos das duas camadas num score 0-100
comparável, com foco em momentum de curto/médio prazo (não em "value investing").

Isto é um ranking de probabilidade relativa, não uma previsão. Mercados cripto são
extremamente voláteis e passado não garante futuro.
"""
import math

from . import config


def _clamp(value, low, high):
    if value is None:
        return low
    return max(low, min(high, value))


def _sigmoid_scale(x, midpoint, steepness):
    """Mapeia um valor não limitado para 0-100 com uma curva suave (evita outliers absurdos dominarem)."""
    if x is None:
        return 0
    try:
        return 100 / (1 + math.exp(-steepness * (x - midpoint)))
    except OverflowError:
        return 100.0 if x > midpoint else 0.0


def score_cex_candidate(c):
    w = config.CEX_WEIGHTS
    s_1h = _sigmoid_scale(c.get("chg_1h"), midpoint=1.5, steepness=0.6)
    s_24h = _sigmoid_scale(c.get("chg_24h"), midpoint=6, steepness=0.15)
    s_7d = _sigmoid_scale(c.get("chg_7d"), midpoint=10, steepness=0.08)
    s_turnover = _sigmoid_scale(c.get("turnover"), midpoint=0.4, steepness=4)

    score = (
        w["chg_1h"] * s_1h
        + w["chg_24h"] * s_24h
        + w["turnover"] * s_turnover
        + w["chg_7d"] * s_7d
    )
    return round(_clamp(score, 0, 100), 1)


def _implied_pre_window_move_pct(chg_6h, chg_24h):
    """
    Aproxima a variação de preço que já tinha acontecido ANTES da janela de rutura (as
    últimas 6h), a partir de dois valores já recolhidos (chg_6h, chg_24h), sem OHLCV extra:

        P_agora / P_há_6h  = 1 + chg_6h/100
        P_agora / P_há_24h = 1 + chg_24h/100
        => P_há_6h / P_há_24h = (1 + chg_24h/100) / (1 + chg_6h/100)

    Um resultado próximo de 0% indica pouca variação nas ~18h antes da janela recente (uma
    "base" calma); um valor grande indica que o movimento principal já tinha acontecido antes
    da fotografia atual (rutura "stale", já em reversão).
    """
    if chg_6h is None or chg_24h is None:
        return None
    denom = 1 + chg_6h / 100
    if denom == 0:
        return None
    return ((1 + chg_24h / 100) / denom - 1) * 100


def detect_base_breakout(c):
    """
    Autoanálise 2026-09-12: distingue uma rutura genuína (preço relativamente estável antes,
    rutura de preço agora — ex. EMBER/CME/UP trazidos pelo Ricardo) de um pump que É o próprio
    nascimento do token (ex. BNC4 — sobe quase verticalmente nos primeiros candles de vida e
    não tem qualquer "base" prévia para medir). Não prevê se a rutura vai sustentar-se ou
    reverter (isso fica a cargo do stop-loss/trailing stop na saída) — só identifica a forma
    do movimento, para o score deixar de tratar as duas situações da mesma maneira.

    Devolve None quando não há histórico suficiente para confiar no chg_24h (pool demasiado
    nova), ou {"is_breakout": bool, "pre_window_move_pct": float} caso contrário.
    """
    age = c.get("pool_age_minutes")
    if age is None or age < config.DEX_BREAKOUT_MIN_POOL_AGE_HOURS * 60:
        return None

    pre_move = _implied_pre_window_move_pct(c.get("chg_6h"), c.get("chg_24h"))
    if pre_move is None:
        return None

    is_breakout = (
        abs(pre_move) <= config.DEX_BREAKOUT_MAX_PRE_WINDOW_MOVE_PCT
        and (c.get("chg_6h") or 0) >= config.DEX_BREAKOUT_MIN_RECENT_MOVE_PCT
    )
    return {"is_breakout": is_breakout, "pre_window_move_pct": round(pre_move, 1)}


def score_dex_candidate(c):
    w = config.DEX_WEIGHTS
    liq = c.get("liquidity_usd") or 0
    vol = c.get("volume_24h") or 0
    vol_liq_ratio = (vol / liq) if liq else 0

    s_1h = _sigmoid_scale(c.get("chg_1h"), midpoint=8, steepness=0.12)
    s_6h = _sigmoid_scale(c.get("chg_6h"), midpoint=15, steepness=0.07)
    s_vol_liq = _sigmoid_scale(vol_liq_ratio, midpoint=1.5, steepness=1.2)
    s_boosted = 100 if c.get("boosted") else 0

    breakout = detect_base_breakout(c)
    c["base_breakout"] = breakout  # guardado para auditoria/telegram, não só para o score
    s_base_breakout = 100 if (breakout and breakout["is_breakout"]) else 0

    score = (
        w["chg_1h"] * s_1h
        + w["chg_6h"] * s_6h
        + w["vol_liq_ratio"] * s_vol_liq
        + w["boosted_bonus"] * s_boosted
        + w["base_breakout"] * s_base_breakout
    )

    # penalização leve se a segurança não foi confirmada (não elimina, apenas desconta)
    security = c.get("security") or {}
    if not security.get("checked"):
        score *= 0.9

    return round(_clamp(score, 0, 100), 1)


def passes_hard_filters(c):
    """Filtros eliminatórios — falhar qualquer um destes remove o candidato por completo."""
    if c["tier"] == "dex_micro_cap":
        liq = c.get("liquidity_usd") or 0
        vol = c.get("volume_24h") or 0
        mcap = c.get("market_cap") or 0
        age = c.get("pool_age_minutes")

        if liq < config.DEX_MIN_LIQUIDITY_USD:
            return False
        if vol < config.DEX_MIN_VOLUME_24H_USD:
            return False
        if mcap and mcap > config.DEX_MAX_FDV_USD:
            return False
        if age is not None and age < config.DEX_MIN_POOL_AGE_MINUTES:
            return False

        security = c.get("security") or {}
        if security.get("checked") and not security.get("safe"):
            return False  # gate eliminatório real: honeypot/red flag confirmada

        # Autoanálise 2026-08-30: "não verificado" estava a passar como se fosse "seguro"
        # (só um desconto de 10% no score, insuficiente — TRUMPSTACY tinha score 90 mesmo
        # sem dados GoPlus e colapsou -99,5%). Um gate "eliminatório" não pode aceitar
        # candidatos que nunca chegaram a ser verificados.
        if not security.get("checked"):
            return False

    return True


def score_and_rank(candidates):
    scored = []
    for c in candidates:
        if not passes_hard_filters(c):
            continue
        c["score"] = score_cex_candidate(c) if c["tier"] == "cex_small_cap" else score_dex_candidate(c)
        if c["score"] >= config.MIN_SCORE_TO_ALERT:
            scored.append(c)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
