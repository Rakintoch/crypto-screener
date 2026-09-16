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
from . import fx
from . import lessons
from . import playbook
from . import scoring
from . import sources_coingecko
from . import sources_dexscreener
from . import telegram_alert

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
            f"{config.PORTFOLIO_STATE_FILE} exists but isn't valid JSON ({e}); "
            "the run was aborted instead of silently resetting the challenge."
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

        # Autoanálise 2026-09-13 (pedido do Ricardo): guarda o Market Cap (ou FDV, para
        # tokens DEX novos, como já acontece nos alertas de descoberta) devolvido pela mesma
        # chamada que já reavalia o preço — para o mostrar no relatório do portfólio sem
        # precisar de outra chamada à API.
        mcap = fresh.get("market_cap")
        if mcap:
            pos["last_market_cap"] = mcap

        fresh["security"] = {"checked": True, "safe": True, "notes": "position already vetted at entry"}
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


def _check_exits(state, now, force_all=False):
    actions = []
    for key, pos in list(state["positions"].items()):
        entry_price = pos["entry_price_eur"]
        last_price = pos.get("last_price_eur", entry_price)
        change_pct = (last_price - entry_price) / entry_price if entry_price else 0
        target_pct = config.TAKE_PROFIT_PCT.get(pos["tier"], 0.25)

        reason = None
        exit_price = last_price

        if force_all:
            reason = f"end of challenge ({config.CHALLENGE_DURATION_DAYS} days) — forced liquidation"
        elif pos.get("missed_updates", 0) >= MISSED_UPDATES_BEFORE_ASSUMED_RUG:
            reason = "no market data across consecutive runs — assumed near-total loss (possible rug)"
            exit_price = entry_price * ASSUMED_RUG_RECOVERY_PCT
        elif pos.get("trailing_active"):
            # Autoanálise 2026-09-12 (ver nota em config.py): o pico é persistido na posição e
            # reavaliado em CADA corrida, sem prazo fixo — só fecha com um recuo real desde o
            # pico mais alto já visto, para não cortar uma corrida sustentada cedo demais.
            pos["trailing_peak_eur"] = max(pos.get("trailing_peak_eur", last_price), last_price)
            peak = pos["trailing_peak_eur"]
            drawdown_from_peak = (peak - last_price) / peak if peak else 0
            if drawdown_from_peak >= config.TRAILING_STOP_DRAWDOWN_PCT:
                peak_change = (peak / entry_price - 1) if entry_price else 0
                reason = (
                    f"trailing stop hit — dropped {drawdown_from_peak:.1%} from the peak "
                    f"(peak {peak_change:+.1%} since entry)"
                )
                exit_price = last_price
            # senão: continua acima do gatilho de recuo — mantém a posição aberta, sem ação
        elif change_pct >= target_pct:
            if config.TRAILING_STOP_ENABLED:
                pos["trailing_active"] = True
                pos["trailing_peak_eur"] = last_price
                # não fecha nesta corrida — passa a perseguir o pico nas corridas seguintes
            else:
                reason = f"take-profit hit ({change_pct:+.1%})"
        elif change_pct <= config.STOP_LOSS_PCT.get(pos["tier"], -0.15):
            reason = f"stop-loss hit ({change_pct:+.1%})"
        elif pos.get("last_score") is not None and pos["last_score"] < config.SCORE_DECAY_EXIT:
            reason = f"momentum thesis invalidated (score dropped to {pos['last_score']:.0f})"

        if reason:
            trade = _close_position(state, key, exit_price, reason, now)
            actions.append({"action": "sell", **trade})

    return actions


def _alert_capital_protection(state, equity):
    """Aviso único (não repete em cada corrida) quando o disjuntor de capital é acionado —
    ver config.MAX_DRAWDOWN_HALT_PCT. Mostra o valor em USD (pedido do Ricardo 2026-09-13) —
    a contabilidade interna continua em EUR, só a apresentação muda (ver fx.py/telegram_alert.py)."""
    pnl_pct = (equity / state["starting_balance_eur"] - 1) * 100
    rate = state.get("last_eur_rate") or fx.FALLBACK_USD_TO_EUR
    equity_usd = equity / rate
    msg = (
        "🛑 *Capital protection activated*\n\n"
        f"Total balance dropped to ${equity_usd:,.2f} ({pnl_pct:+.1f}% since inception), "
        f"reaching the protection threshold ({config.MAX_DRAWDOWN_HALT_PCT:+.0%} of the starting balance).\n\n"
        "From now on the bot stops opening new positions — the remaining capital stays in "
        "cash, protected from further risk. Positions already open continue to be monitored "
        "normally (take-profit/stop-loss/trailing stop) and the challenge continues until the "
        "end of the 10 days."
    )
    telegram_alert.send_telegram_message(msg)


def _robustness_proxy(c):
    """Autoanálise 2026-09-13: o desempate por liquidez do SELECTION_SCORE_CEILING
    (ver comentário abaixo, em _check_entries) estava, na prática, inerte para o tier
    cex_small_cap — sources_coingecko.py nunca preenche "liquidity_usd" para candidatos
    CEX (só os candidatos DEX, via sources_dexscreener.py, trazem liquidez real). Como
    quase todas as posições do desafio são cex_small_cap, o desempate nunca chegava a
    atuar onde mais importava. Esta função mantém o comportamento atual para candidatos
    com liquidity_usd real (DEX) e usa volume_24h como proxy de robustez/liquidez quando
    liquidity_usd não está disponível (CEX)."""
    liquidity = c.get("liquidity_usd")
    if liquidity is not None:
        return liquidity
    return c.get("volume_24h") or 0


def _maybe_rotate_weak_position(state, now):
    """Liberta UMA vaga, fechando antecipadamente a posição mais fraca, quando isso é a
    única forma de aproveitar um candidato novo já qualificado — ver a nota em config.py
    (secção "Rotação de posições fracas") para o raciocínio completo por trás do critério.

    Deliberadamente conservador: só mexe numa posição que já mostra decadência real (score a
    cair para perto de SCORE_DECAY_EXIT) E sem qualquer ganho não realizado, já aberta há pelo
    menos ROTATION_MIN_HOLD_HOURS, e nunca mais de uma vez a cada ROTATION_MIN_INTERVAL_HOURS
    (cooldown global, para não gerar rotação em cadeia/churn). Não mexe numa posição já a
    perseguir um trailing stop (`trailing_active`) — essa já está a proteger um ganho real."""
    if state.get("last_rotation_ts") and (now - state["last_rotation_ts"]) < config.ROTATION_MIN_INTERVAL_HOURS * 3600:
        return None

    candidates_to_rotate = [
        (key, pos) for key, pos in state["positions"].items()
        if pos.get("last_score") is not None
        and pos["last_score"] < config.ROTATION_SCORE_WATCH
        and not pos.get("trailing_active")
        and pos.get("last_price_eur", pos["entry_price_eur"]) <= pos["entry_price_eur"]
        and (now - pos["entry_ts"]) >= config.ROTATION_MIN_HOLD_HOURS * 3600
    ]
    if not candidates_to_rotate:
        return None

    # a mais fraca primeiro (score mais baixo) — não a mais recente nem a de maior prejuízo
    key, pos = min(candidates_to_rotate, key=lambda kp: kp[1]["last_score"])
    last_price = pos.get("last_price_eur", pos["entry_price_eur"])
    trade = _close_position(
        state, key, last_price,
        f"rotated out — thesis fading (score {pos['last_score']:.0f}, no unrealized gain) "
        "to free a slot for a new qualified candidate",
        now,
    )
    state["last_rotation_ts"] = now
    return {"action": "sell", **trade}


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
    # Autoanálise 2026-09-10: ordenar sempre pelo score bruto favorecia sistematicamente o
    # candidato mais "esticado" (que mais subiu, mais depressa) quando vários passam o
    # limiar ao mesmo tempo — e os dados dos 79 trades fechados mostram que isso não ajudou
    # (ver config.SELECTION_SCORE_CEILING). Acima do teto, deixa de desempatar por score —
    # passa a desempatar por liquidez (candidato mais líquido/robusto primeiro), em vez de
    # continuar a preferir cegamente quem subiu mais.
    eligible.sort(
        key=lambda c: (min(c["score"], config.SELECTION_SCORE_CEILING), _robustness_proxy(c)),
        reverse=True,
    )

    slots_free = config.MAX_CONCURRENT_POSITIONS - len(state["positions"])
    if slots_free <= 0:
        # Antes de desistir, vê se vale a pena libertar UMA vaga (ver config.py, secção
        # "Rotação de posições fracas") — só quando há mesmo um candidato qualificado à
        # espera, para não fechar uma posição a decair sem ter para onde canalizar o capital.
        if config.ROTATION_ENABLED and eligible:
            rotated = _maybe_rotate_weak_position(state, now)
            if rotated:
                actions.append(rotated)
                slots_free = config.MAX_CONCURRENT_POSITIONS - len(state["positions"])
        if slots_free <= 0:
            return actions

    for c in eligible[:slots_free]:
        equity = _equity(state)
        size_pct = config.POSITION_SIZE_PCT_BY_TIER.get(c["tier"], config.POSITION_SIZE_PCT_OF_EQUITY)
        size_eur = min(equity * size_pct, state["cash_eur"])
        if size_eur < config.MIN_TRADE_EUR:
            continue

        entry_price_eur = c["price_usd"] * c["_eur_rate"]
        qty = size_eur / entry_price_eur

        # Pedido do Ricardo 2026-09-14 (caso SOXSB): o preço guardado acima é uma média do
        # CoinGecko entre várias venues, mas uma compra real só pode ser executada numa de
        # cada vez — só faz sentido para o tier cex_small_cap, onde essa ambiguidade existe
        # (o tier dex_micro_cap já vem de UMA pool específica, sem essa média). Uma chamada
        # extra por compra real (não por candidato avaliado); nunca bloqueia a compra se
        # falhar — a nota fica simplesmente ausente do alerta.
        entry_venue = None
        # Endereço do contrato — pedido do Ricardo 2026-09-14, para mostrar na listagem de
        # posições abertas. Só faz sentido buscar para cex_small_cap: dex_micro_cap já vem
        # de uma pool/token on-chain específico, cujo próprio id (c["id"]) já É o endereço,
        # sem chamada extra. Chamada independente da de entry_venue (falhas isoladas não se
        # afetam mutuamente) — nunca bloqueia a compra.
        entry_contract_address = None
        if c["tier"] == "cex_small_cap":
            try:
                entry_venue = sources_coingecko.fetch_top_venue(c["id"])
            except Exception:
                traceback.print_exc()
            try:
                entry_contract_address = sources_coingecko.fetch_contract_address(c["id"])
            except Exception:
                traceback.print_exc()

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
            # Market Cap (ou FDV, para tokens DEX) no momento da entrada — pedido do Ricardo
            # 2026-09-14: as mensagens do Telegram passam a mostrar o MC em vez do preço
            # unitário da moeda em cada compra, tanto aqui como no Pump Watch.
            "entry_market_cap": c.get("market_cap"),
            # Venue (exchange) mais líquida usada como referência do preço de entrada —
            # pedido do Ricardo 2026-09-14, ver comentário acima. None para dex_micro_cap
            # (já é uma pool única) ou se a chamada extra falhar.
            "entry_venue": entry_venue,
            # Endereço do contrato on-chain — pedido do Ricardo 2026-09-14, ver comentário
            # acima. None para moedas cex_small_cap nativas de uma chain própria (ex: ARDR)
            # ou se a chamada extra tiver falhado; para dex_micro_cap não é guardado aqui —
            # a listagem usa o próprio "id" da posição (ver telegram_alert.py).
            "entry_contract_address": entry_contract_address,
            # snapshot dos critérios de entrada — usado depois pelo lessons.py se a posição
            # vier a fechar com prejuízo, para a lição referenciar o que passou nos filtros
            "entry_score": c["score"],
            "entry_liquidity_usd": c.get("liquidity_usd"),
            "entry_volume_24h_usd": c.get("volume_24h"),
            "entry_security_notes": (c.get("security") or {}).get("notes"),
            # Componentes brutos do score na entrada (adicionado 2026-09-10) — a autoanálise
            # de fim de desafio só conseguiu provar que o score FINAL não prevê o resultado
            # (correlação -0,019 em 79 trades), mas não conseguiu decompor QUAL sinal é o
            # culpado, porque só o score já combinado ficava guardado. Isto guarda os sinais
            # de origem (None nos que não se aplicam a esta camada) para a próxima autoanálise
            # conseguir mesmo isolar o(s) sinal(is) sem poder preditivo.
            "entry_chg_1h": c.get("chg_1h"),
            "entry_chg_24h": c.get("chg_24h"),
            "entry_chg_7d": c.get("chg_7d"),
            "entry_chg_6h": c.get("chg_6h"),
            "entry_turnover": c.get("turnover"),
            "entry_vol_liq_ratio": (
                (c.get("volume_24h") / c["liquidity_usd"])
                if c.get("liquidity_usd") else None
            ),
            "entry_boosted": c.get("boosted"),
            # Autoanálise 2026-09-12 (ver scoring.detect_base_breakout / config.DEX_WEIGHTS
            # "base_breakout"): a idade da pool na entrada NUNCA tinha sido guardada no
            # snapshot — impossibilitou confirmar, ao analisar o histórico de trades já
            # fechados, se o novo sinal de "base estável seguida de rutura" teria feito
            # diferença (dois dos três trades DEX fechados até agora, Stunk e revolve, têm
            # entry_chg_6h idêntico a entry_chg_24h, sinal de pools muito jovens — mas sem
            # a idade guardada não dá para confirmar se ficariam de fora pelo gate de
            # DEX_BREAKOUT_MIN_POOL_AGE_HOURS). Guardar isto agora permite à próxima
            # autoanálise validar (ou invalidar) este sinal com dados reais, em vez de só
            # com os quatro exemplos manuais que motivaram a mudança.
            "entry_pool_age_minutes": c.get("pool_age_minutes"),
            "entry_base_breakout": c.get("base_breakout"),
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
    exit_actions = _check_exits(state, now, force_all=force_all)

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
    # guarda a taxa de câmbio desta corrida, para as mensagens Telegram (USD) e o /status
    # (que não volta a chamar a API de câmbio) terem sempre uma taxa recente para converter
    state["last_eur_rate"] = eur_rate

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

    # guarda a taxa de câmbio desta corrida, para as mensagens Telegram (USD) e o /status
    # (que não volta a chamar a API de câmbio) terem sempre uma taxa recente para converter
    state["last_eur_rate"] = eur_rate

    exit_actions, final_report = _process_exits(state, now, eur_rate)

    state["last_run_ts"] = now
    save_portfolio(state)

    return state, exit_actions, final_report
