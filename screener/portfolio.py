"""
Desafio de portfólio virtual: gere um saldo simulado de STARTING_BALANCE_EUR, com dados de
mercado reais, durante CHALLENGE_DURATION_DAYS a partir da primeira compra virtual executada.

Isto NUNCA movimenta dinheiro real. Não há chaves de exchange, não há wallet, não há
assinatura de transações — é um livro-razão (data/portfolio_state.json) que regista preços
reais e simula compras/vendas segundo regras fixas de gestão de risco (take-profit,
stop-loss, invalidação de tese por queda de score, e liquidação forçada ao fim de 10 dias).
"""
import time
import traceback

from . import config
from . import scoring
from . import sources_coingecko
from . import sources_dexscreener

MISSED_UPDATES_BEFORE_ASSUMED_RUG = 3
ASSUMED_RUG_RECOVERY_PCT = 0.05  # assume que só sobra 5% do valor se o token deixar de ter dados


def _default_state():
    return {
        "status": "not_started",   # not_started -> active -> finished
        "start_ts": None,
        "end_ts": None,
        "cash_eur": config.STARTING_BALANCE_EUR,
        "starting_balance_eur": config.STARTING_BALANCE_EUR,
        "positions": {},
        "closed_trades": [],
        "last_run_ts": None,
    }


def load_portfolio():
    import json
    import os
    if not os.path.exists(config.PORTFOLIO_STATE_FILE):
        return _default_state()
    try:
        with open(config.PORTFOLIO_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            if not state:
                return _default_state()
            return state
    except Exception:
        return _default_state()


def save_portfolio(state):
    import json
    import os
    os.makedirs(os.path.dirname(config.PORTFOLIO_STATE_FILE), exist_ok=True)
    with open(config.PORTFOLIO_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _equity(state):
    open_value = sum(p["qty"] * p.get("last_price_eur", p["entry_price_eur"]) for p in state["positions"].values())
    return state["cash_eur"] + open_value


def _reprice_positions(state, eur_rate):
    """Atualiza last_price_eur / last_score de cada posição aberta com dados frescos."""
    positions = state["positions"]
    if not positions:
        return

    cex_ids = [p["id"] for p in positions.values() if p["tier"] == "cex_small_cap"]
    dex_addrs = [p["id"] for p in positions.values() if p["tier"] == "dex_micro_cap"]

    fresh_cex = {}
    fresh_dex = {}
    try:
        if cex_ids:
            fresh_cex = sources_coingecko.fetch_by_ids(cex_ids)
    except Exception:
        traceback.print_exc()
    try:
        if dex_addrs:
            for rec in sources_dexscreener.fetch_market_data_for_addresses(dex_addrs):
                fresh_dex[rec["id"]] = rec
    except Exception:
        traceback.print_exc()

    for key, pos in positions.items():
        fresh = fresh_cex.get(pos["id"]) if pos["tier"] == "cex_small_cap" else fresh_dex.get(pos["id"])
        if fresh is None:
            pos["missed_updates"] = pos.get("missed_updates", 0) + 1
            continue

        pos["missed_updates"] = 0
        price_usd = fresh.get("price_usd")
        if price_usd:
            pos["last_price_eur"] = price_usd * eur_rate

        fresh["security"] = {"checked": True, "safe": True, "notes": "posição já vetada na entrada"}
        pos["last_score"] = scoring.score_cex_candidate(fresh) if pos["tier"] == "cex_small_cap" else scoring.score_dex_candidate(fresh)


def _close_position(state, key, exit_price_eur, reason, now):
    pos = state["positions"].pop(key)
    proceeds = pos["qty"] * exit_price_eur
    state["cash_eur"] += proceeds
    pnl_eur = proceeds - pos["cost_eur"]
    pnl_pct = (pnl_eur / pos["cost_eur"]) * 100 if pos["cost_eur"] else 0

    trade = dict(pos)
    trade.update({
        "exit_price_eur": exit_price_eur,
        "exit_ts": now,
        "exit_reason": reason,
        "proceeds_eur": proceeds,
        "pnl_eur": pnl_eur,
        "pnl_pct": pnl_pct,
    })
    state["closed_trades"].append(trade)
    return trade


def _check_exits(state, now, force_all=False):
    actions = []
    for key, pos in list(state["positions"].items()):
        entry_price = pos["entry_price_eur"]
        last_price = pos.get("last_price_eur", entry_price)
        change_pct = (last_price - entry_price) / entry_price if entry_price else 0

        reason = None
        exit_price = last_price

        if force_all:
            reason = f"fim do desafio ({config.CHALLENGE_DURATION_DAYS} dias) — liquidação forçada"
        elif pos.get("missed_updates", 0) >= MISSED_UPDATES_BEFORE_ASSUMED_RUG:
            reason = "sem dados de mercado em corridas sucessivas — assumida perda quase total (possível rug)"
            exit_price = entry_price * ASSUMED_RUG_RECOVERY_PCT
        elif change_pct >= config.TAKE_PROFIT_PCT.get(pos["tier"], 0.25):
            reason = f"take-profit atingido ({change_pct:+.1%})"
        elif change_pct <= config.STOP_LOSS_PCT.get(pos["tier"], -0.15):
            reason = f"stop-loss atingido ({change_pct:+.1%})"
        elif pos.get("last_score") is not None and pos["last_score"] < config.SCORE_DECAY_EXIT:
            reason = f"tese de momentum invalidada (score caiu para {pos['last_score']:.0f})"

        if reason:
            trade = _close_position(state, key, exit_price, reason, now)
            actions.append({"action": "sell", **trade})

    return actions


def _check_entries(state, ranked_candidates, now):
    actions = []
    slots_free = config.MAX_CONCURRENT_POSITIONS - len(state["positions"])
    if slots_free <= 0:
        return actions

    held_keys = set(state["positions"].keys())
    recent_exit_ids = {
        f"{t['tier']}:{t['id']}" for t in state["closed_trades"]
        if (now - t["exit_ts"]) < 6 * 3600
    }

    eligible = [
        c for c in ranked_candidates
        if c["score"] >= config.ENTRY_MIN_SCORE
        and f"{c['tier']}:{c['id']}" not in held_keys
        and f"{c['tier']}:{c['id']}" not in recent_exit_ids
        and c.get("price_usd")
    ]
    eligible.sort(key=lambda c: c["score"], reverse=True)

    for c in eligible[:slots_free]:
        equity = _equity(state)
        size_eur = min(equity * config.POSITION_SIZE_PCT_OF_EQUITY, state["cash_eur"])
        if size_eur < config.MIN_TRADE_EUR:
            continue

        entry_price_eur = c["price_usd"] * c["_eur_rate"]
        qty = size_eur / entry_price_eur

        key = f"{c['tier']}:{c['id']}"
        state["positions"][key] = {
            "tier": c["tier"],
            "id": c["id"],
            "symbol": c["symbol"],
            "network": c.get("network"),
            "url": c.get("url"),
            "qty": qty,
            "entry_price_eur": entry_price_eur,
            "entry_ts": now,
            "cost_eur": size_eur,
            "last_price_eur": entry_price_eur,
            "last_score": c["score"],
            "missed_updates": 0,
        }
        state["cash_eur"] -= size_eur

        if state["status"] == "not_started":
            state["status"] = "active"
            state["start_ts"] = now
            state["end_ts"] = now + config.CHALLENGE_DURATION_DAYS * 86400

        actions.append({"action": "buy", **state["positions"][key]})

    return actions


def run_portfolio_cycle(ranked_candidates, eur_rate):
    """
    Executa um ciclo completo: reavalia posições abertas, fecha o que deve ser fechado
    (incluindo liquidação forçada se o desafio de 10 dias terminou), e abre novas posições
    se houver capital e candidatos suficientemente fortes. Devolve (state, actions,
    final_report_or_None).
    """
    state = load_portfolio()
    now = time.time()

    if state["status"] == "finished":
        # desafio já terminado — não faz mais nada, apenas devolve o estado tal como está
        return state, [], None

    _reprice_positions(state, eur_rate)

    force_all = state["status"] == "active" and state["end_ts"] is not None and now >= state["end_ts"]
    exit_actions = _check_exits(state, now, force_all=force_all)

    entry_actions = []
    if not force_all:
        for c in ranked_candidates:
            c["_eur_rate"] = eur_rate
        entry_actions = _check_entries(state, ranked_candidates, now)

    final_report = None
    if force_all:
        state["status"] = "finished"
        final_equity = state["cash_eur"]  # tudo já foi liquidado em _check_exits
        final_report = {
            "starting_balance_eur": state["starting_balance_eur"],
            "final_balance_eur": final_equity,
            "pnl_eur": final_equity - state["starting_balance_eur"],
            "pnl_pct": (final_equity - state["starting_balance_eur"]) / state["starting_balance_eur"] * 100,
            "num_trades": len(state["closed_trades"]),
            "start_ts": state["start_ts"],
            "end_ts": state["end_ts"],
        }

    state["last_run_ts"] = now
    save_portfolio(state)

    return state, exit_actions + entry_actions, final_report
