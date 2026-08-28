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


def score_dex_candidate(c):
    w = config.DEX_WEIGHTS
    liq = c.get("liquidity_usd") or 0
    vol = c.get("volume_24h") or 0
    vol_liq_ratio = (vol / liq) if liq else 0

    s_1h = _sigmoid_scale(c.get("chg_1h"), midpoint=8, steepness=0.12)
    s_6h = _sigmoid_scale(c.get("chg_6h"), midpoint=15, steepness=0.07)
    s_vol_liq = _sigmoid_scale(vol_liq_ratio, midpoint=1.5, steepness=1.2)
    s_boosted = 100 if c.get("boosted") else 0

    score = (
        w["chg_1h"] * s_1h
        + w["chg_6h"] * s_6h
        + w["vol_liq_ratio"] * s_vol_liq
        + w["boosted_bonus"] * s_boosted
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
