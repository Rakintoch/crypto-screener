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
from . import lessons
from . import playbook
from . import scoring
from . import sources_coingecko
from . import sources_dexscreener
from . import telegram_alert

MISSED_UPDATES_BEFORE_ASSUMED_RUG = 3
ASSUMED_RUG_RECOVERY_PCT = 0.05  # assume que só sobra 5% do valor se o token deixar de ter dados


def _fetch_fresh_price_eur(pos, eur_rate):
    """Vai buscar um preço fresco (uma única posição) para usar durante a vigilância de
    trailing stop. Devolve None se não conseguir (mantém o último preço conhecido nesse caso)."""
    try:
        if pos["tier"] == "cex_small_cap":
            fresh = sources_coingecko.fetch_by_ids([pos["id"]])
            rec = fresh.get(pos["id"])
        else:
            recs = sources_dexscreener.fetch_market_data_for_addresses([pos["id"]])
            rec = recs[0] if recs else None
        if rec and rec.get("price_usd"):
            return rec["price_usd"] * eur_rate
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return None


def _watch_trailing_stop(pos, entry_price, target_price, eur_rate, now):
    """
    Chamado assim que uma posição atinge o take-profit. Em vez de vender de imediato, vigia
    o preço durante TRAILING_STOP_WINDOW_SECONDS (verificando a cada TRAILING_STOP_CHECK_
    INTERVAL_SECONDS) para tentar apanhar mais da subida, mas sai ao primeiro sinal real de
    reversão. Devolve (exit_price_eur, reason).
    """
    peak_price = target_price
    last_price = target_price
    elapsed = 0

    while elapsed < config.TRAILING_STOP_WINDOW_SECONDS:
        time.sleep(config.TRAILING_STOP_CHECK_INTERVAL_SECONDS)
        elapsed += config.TRAILING_STOP_CHECK_INTERVAL_SECONDS

        fresh_price = _fetch_fresh_price_eur(pos, eur_rate)
        if fresh_price is None:
            continue  # falha pontual da API — mantém a vigilância, tenta de novo na próxima volta
        last_price = fresh_price
        peak_price = max(peak_price, fresh_price)

        drawdown_from_peak = (peak_price - fresh_price) / peak_price if peak_price else 0
        change_from_entry = (fresh_price - entry_price) / entry_price if entry_price else 0
        target_pct = config.TAKE_PROFIT_PCT.get(pos["tier"], 0.25)

        if drawdown_from_peak >= config.TRAILING_STOP_DRAWDOWN_PCT:
            return fresh_price, (
                f"trailing stop atingido — caiu {drawdown_from_peak:.1%} desde o pico "
                f"(pico {(peak_price / entry_price - 1):+.1%} desde a entrada)"
            )
        if change_from_entry <= target_pct:
            return fresh_price, (
                f"trailing stop — recuou até ao valor-alvo ({change_from_entry:+.1%}) "
                f"depois de um pico de {(peak_price / entry_price - 1):+.1%}"
            )

    return last_price, (
        f"take-profit atingido após vigilância de {config.TRAILING_STOP_WINDOW_SECONDS // 60} min "
        f"(pico {(peak_price / entry_price - 1):+.1%} desde a entrada)"
    )


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


class PortfolioStateCorrupted(Exception):
    """Levantado quando data/portfolio_state.json existe mas não é JSON válido.

    Incidente de 2026-08-31: um "git pull --rebase --autostash" com conflito (entre o
    screener.yml a cada 2h e o bot_listener.yml a cada 15 min a escrever no mesmo ficheiro)
    deixou marcadores de conflito ("<<<<<<< Updated upstream" etc.) commitados no ficheiro.
    Como load_portfolio() antes tratava QUALQUER falha de leitura como "sem estado" e devolvia
    silenciosamente um estado por omissão (saldo cheio, sem posições), isso reiniciou o
    desafio de 10 dias sem qualquer decisão nesse sentido. Agora, em vez de mascarar o
    problema, é levantada esta exceção — quem chama (run_portfolio_cycle/run_exit_check_cycle)
    aborta a corrida, avisa no Telegram e NÃO grava nada por cima do ficheiro corrompido,
    para que possa ser recuperado manualmente a partir do histórico do Git."""


def load_portfolio():
    import json
    import os
    if not os.path.exists(config.PORTFOLIO_STATE_FILE):
        return _default_state()
    try:
        with open(config.PORTFOLIO_STATE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return _default_state()
        return json.loads(content)
    except Exception as e:
        raise PortfolioStateCorrupted(
            f"{config.PORTFOLIO_STATE_FILE} existe mas não é JSON válido ({e}); "
            "a corrida foi abortada em vez de reiniciar o desafio silenciosamente."
        ) from e


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
    lessons.record_if_lesson(trade)
    playbook.record_if_win(trade)
    return trade


def _check_exits(state, now, eur_rate=None, force_all=False):
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
            if config.TRAILING_STOP_ENABLED and eur_rate:
                exit_price, reason = _watch_trailing_stop(pos, entry_price, last_price, eur_rate, now)
            else:
                reason = f"take-profit atingido ({change_pct:+.1%})"
        elif change_pct <= config.STOP_LOSS_PCT.get(pos["tier"], -0.15):
            reason = f"stop-loss atingido ({change_pct:+.1%})"
        elif pos.get("last_score") is not None and pos["last_score"] < config.SCORE_DECAY_EXIT:
            reason = f"tese de momentum invalidada (score caiu para {pos['last_score']:.0f})"

        if reason:
            trade = _close_position(state, key, exit_price, reason, now)
            actions.append({"action": "sell", **trade})

    return actions


def _alert_capital_protection(state, equity):
    """Aviso único (não repete em cada corrida) quando o disjuntor de capital é acionado —
    ver config.MAX_DRAWDOWN_HALT_PCT."""
    pnl_pct = (equity / state["starting_balance_eur"] - 1) * 100
    msg = (
        "🛑 *Proteção de capital ativada*\n\n"
        f"O saldo total caiu para {equity:.2f}€ ({pnl_pct:+.1f}% desde o início), "
        f"atingindo o limiar de proteção ({config.MAX_DRAWDOWN_HALT_PCT:+.0%} do saldo inicial).\n\n"
        "A partir de agora o bot deixa de abrir posições novas — o capital que resta fica em "
        "cash, protegido de mais risco. As posições já abertas continuam a ser vigiadas "
        "normalmente (take-profit/stop-loss/trailing stop) e o desafio prossegue até ao fim "
        "dos 10 dias."
    )
    telegram_alert.send_telegram_message(msg)


def _check_entries(state, ranked_candidates, now):
    actions = []

    equity = _equity(state)
    floor = state["starting_balance_eur"] * (1 + config.MAX_DRAWDOWN_HALT_PCT)
    if not state.get("capital_protection_active") and equity <= floor:
        state["capital_protection_active"] = True
        state["capital_protection_ts"] = now
        _alert_capital_protection(state, equity)
    if state.get("capital_protection_active"):
        return actions  # disjuntor acionado: não abre posições novas (ver config.MAX_DRAWDOWN_HALT_PCT)

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
        size_pct = config.POSITION_SIZE_PCT_BY_TIER.get(c["tier"], config.POSITION_SIZE_PCT_OF_EQUITY)
        size_eur = min(equity * size_pct, state["cash_eur"])
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
            # snapshot dos critérios de entrada — usado depois pelo lessons.py se a posição
            # vier a fechar com prejuízo, para a lição referenciar o que passou nos filtros
            "entry_score": c["score"],
            "entry_liquidity_usd": c.get("liquidity_usd"),
            "entry_volume_24h_usd": c.get("volume_24h"),
            "entry_security_notes": (c.get("security") or {}).get("notes"),
        }
        state["cash_eur"] -= size_eur

        if state["status"] == "not_started":
            state["status"] = "active"
            state["start_ts"] = now
            state["end_ts"] = now + config.CHALLENGE_DURATION_DAYS * 86400

        actions.append({"action": "buy", **state["positions"][key]})

    return actions


def _process_exits(state, now, eur_rate):
    """
    Passo partilhado entre o ciclo completo (screener principal) e o monitor leve
    (position_monitor.py): reavalia preços, verifica saídas (incluindo liquidação forçada se
    o desafio de 10 dias terminou) e grava o estado. Não mexe em entradas. Devolve
    (exit_actions, final_report_or_None).
    """
    _reprice_positions(state, eur_rate)

    force_all = state["status"] == "active" and state["end_ts"] is not None and now >= state["end_ts"]
    exit_actions = _check_exits(state, now, eur_rate=eur_rate, force_all=force_all)

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

    return exit_actions, final_report


def run_portfolio_cycle(ranked_candidates, eur_rate):
    """
    Executa um ciclo completo (screener principal, a cada 2h): reavalia posições abertas,
    fecha o que deve ser fechado, e abre novas posições se houver capital e candidatos
    suficientemente fortes. Devolve (state, actions, final_report_or_None).
    """
    state = load_portfolio()
    now = time.time()

    if state["status"] == "finished":
        # desafio já terminado — não faz mais nada, apenas devolve o estado tal como está
        return state, [], None

    exit_actions, final_report = _process_exits(state, now, eur_rate)

    entry_actions = []
    if final_report is None:
        for c in ranked_candidates:
            c["_eur_rate"] = eur_rate
        entry_actions = _check_entries(state, ranked_candidates, now)

    state["last_run_ts"] = now
    save_portfolio(state)

    return state, exit_actions + entry_actions, final_report


def run_exit_check_cycle(eur_rate):
    """
    Ciclo leve (position_monitor.py, correndo dentro do bot_listener.yml a cada poucos
    minutos): só reavalia posições já abertas e verifica saídas — NUNCA abre posições novas
    (isso exige a descoberta cara de candidatos, feita só pelo screener principal a cada 2h).
    Devolve (state, exit_actions, final_report_or_None).
    """
    state = load_portfolio()
    now = time.time()

    if state["status"] != "active" or not state["positions"]:
        return state, [], None

    exit_actions, final_report = _process_exits(state, now, eur_rate)

    state["last_run_ts"] = now
    save_portfolio(state)

    return state, exit_actions, final_report
