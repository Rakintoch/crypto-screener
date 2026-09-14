"""
Pump Watch: sistema experimental, independente do desafio de portfólio virtual principal
(portfolio.py), para testar um sinal de ENTRADA diferente — acumulação, não momentum.

Pedido do Ricardo 2026-09-14: em vez de comprar o que já está a subir (a tese do desafio
principal), este sistema procura sinais de que há volume de compra a acumular-se ANTES do
preço reagir — o padrão clássico pré-rutura ("smart money" a construir posição em silêncio).
Usa OBV (On-Balance Volume) normalizado sobre o histórico horário do CoinGecko como proxy para
essa pressão compradora, já que dados reais ao nível de carteira (quem está a comprar) não são
possíveis de obter de graça com a profundidade necessária — ver sources_coingecko.fetch_market_chart.

Completamente separado do portfolio.py: saldo virtual próprio ($100 equivalente), até
PUMP_WATCH_MAX_POSITIONS (3) posições em simultâneo, saída SEMPRE por trailing stop puro desde
a entrada — sem take-profit fixo, o "pico" arranca no próprio preço de entrada — e uma regra de
gestão de lucro (20% de cada lucro realizado fica reservado, nunca reinvestido). Mantido à parte
para que o ciclo de autoanálise consiga avaliar este sinal isoladamente do desafio de momentum.
Sem prazo fixo (ao contrário do desafio de 10 dias) — é um sistema contínuo de teste de sinal.

Só cex_small_cap por agora (ver nota em config.py) — só reutiliza candidatos CEX já
descobertos pelo screener principal nesta mesma corrida, sem chamadas extra de descoberta.
"""
import json
import os
import time

from . import config
from . import sources_coingecko

class PumpWatchStateCorrupted(Exception):
    """Ver portfolio.PortfolioStateCorrupted — mesmo princípio: nunca mascarar uma falha de
    leitura como 'sem estado', para não perder o histórico deste sistema silenciosamente."""

def _default_state():
    return {
        "status": "not_started",   # not_started -> active (sem "finished" — sistema contínuo)
        "start_ts": None,
        "cash_eur": config.PUMP_WATCH_STARTING_BALANCE_EUR,
        "reserve_eur": 0.0,        # lucro já realizado, retirado de circulação (nunca reinvestido)
        "starting_balance_eur": config.PUMP_WATCH_STARTING_BALANCE_EUR,
        "positions": {},
        "closed_trades": [],
        "last_run_ts": None,
        "last_eur_rate": None,
    }

def load_state():
    if not os.path.exists(config.PUMP_WATCH_STATE_FILE):
        return _default_state()
    try:
        with open(config.PUMP_WATCH_STATE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return _default_state()
        return json.loads(content)
    except Exception as e:
        raise PumpWatchStateCorrupted(
            f"{config.PUMP_WATCH_STATE_FILE} existe mas não é JSON válido ({e}); a corrida foi "
            "abortada em vez de reiniciar silenciosamente este sistema."
        ) from e

def save_state(state):
    os.makedirs(os.path.dirname(config.PUMP_WATCH_STATE_FILE), exist_ok=True)
    with open(config.PUMP_WATCH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def _tradable_equity(state):
    """Capital que conta para dimensionar novas posições — exclui a reserva de lucro, que por
    definição está fora de circulação."""
    open_value = sum(p["qty"] * p.get("last_price_eur", p["entry_price_eur"]) for p in state["positions"].values())
    return state["cash_eur"] + open_value

def _compute_accumulation_signal(prices, volumes):
    """OBV (On-Balance Volume) normalizado — fração do volume total na janela que foi "líquido
    comprador" — mais a variação de preço na mesma janela. prices/volumes: listas [timestamp_ms,
    valor] do CoinGecko market_chart (aproximadamente alinhadas por timestamp)."""
    n = min(len(prices), len(volumes))
    if n < 24:  # menos de ~1 dia de candles horárias — histórico curto demais para confiar
        return None

    closes = [p[1] for p in prices[:n]]
    vols = [v[1] for v in volumes[:n]]

    obv = 0.0
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv += vols[i]
        elif closes[i] < closes[i - 1]:
            obv -= vols[i]

    total_volume = sum(vols) or 1.0
    price_start = closes[0]
    if not price_start:
        return None

    return {
        "obv_score": obv / total_volume,
        "price_change_pct": (closes[-1] - price_start) / price_start,
    }

def _scan_accumulation_candidates(cex_candidates, held_ids, slots_free):
    """Só chama a API de histórico para um shortlist pequeno (pré-filtrado por turnover, um
    critério já disponível sem custo extra) — nunca para todo o universo CEX, para respeitar o
    limite de chamadas partilhado da API pública do CoinGecko."""
    if slots_free <= 0:
        return []

    pool = [c for c in cex_candidates if c.get("id") and c["id"] not in held_ids and c.get("price_usd")]
    pool.sort(key=lambda c: c.get("turnover", 0), reverse=True)
    shortlist = pool[:config.PUMP_WATCH_SHORTLIST_SIZE]

    scored = []
    for c in shortlist:
        chart = sources_coingecko.fetch_market_chart(c["id"], config.PUMP_WATCH_LOOKBACK_DAYS)
        if not chart:
            continue
        signal = _compute_accumulation_signal(chart["prices"], chart["volumes"])
        if not signal:
            continue
        if signal["obv_score"] < config.PUMP_WATCH_MIN_OBV_SCORE:
            continue
        if not (config.PUMP_WATCH_MIN_PRICE_MOVE_PCT <= signal["price_change_pct"] <= config.PUMP_WATCH_MAX_PRICE_MOVE_PCT):
            continue
        scored.append((signal["obv_score"], c))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [c for _, c in scored[:slots_free]]

def _check_entries(state, cex_candidates, eur_rate, now):
    actions = []
    slots_free = config.PUMP_WATCH_MAX_POSITIONS - len(state["positions"])
    if slots_free <= 0:
        return actions

    held_ids = {p["id"] for p in state["positions"].values()}
    picks = _scan_accumulation_candidates(cex_candidates, held_ids, slots_free)

    for c in picks:
        equity = _tradable_equity(state)
        size_eur = min(equity * config.PUMP_WATCH_POSITION_SIZE_PCT, state["cash_eur"])
        if size_eur < config.PUMP_WATCH_MIN_TRADE_EUR:
            continue

        entry_price_eur = c["price_usd"] * eur_rate
        qty = size_eur / entry_price_eur
        key = f"cex_small_cap:{c['id']}"
        state["positions"][key] = {
            "id": c["id"],
            "symbol": c["symbol"],
            "qty": qty,
            "entry_price_eur": entry_price_eur,
            "entry_ts": now,
            "cost_eur": size_eur,
            "last_price_eur": entry_price_eur,
            "peak_price_eur": entry_price_eur,   # o "pico" do trailing arranca no preço de entrada
            # Market Cap no momento da entrada — pedido do Ricardo 2026-09-14: mostrar o MC
            # em vez do preço unitário da moeda nas mensagens de Telegram, aqui e no desafio
            # principal (ver portfolio.py/telegram_alert.py).
            "entry_market_cap": c.get("market_cap"),
            "last_market_cap": c.get("market_cap"),
        }
        state["cash_eur"] -= size_eur

        if state["status"] == "not_started":
            state["status"] = "active"
            state["start_ts"] = now

        actions.append({"action": "buy", **state["positions"][key]})

    return actions

def _close_position(state, key, exit_price_eur, reason, now):
    pos = state["positions"].pop(key)
    proceeds = pos["qty"] * exit_price_eur
    pnl_eur = proceeds - pos["cost_eur"]

    # Gestão de lucro (pedido do Ricardo 2026-09-14): só o LUCRO sofre o corte de 20% para a
    # reserva — uma perda devolve o valor todo ao saldo negociável, para não acelerar a erosão
    # do capital numa sequência de perdas.
    reserved = 0.0
    if pnl_eur > 0:
        reserved = pnl_eur * config.PUMP_WATCH_PROFIT_RESERVE_PCT
        state["reserve_eur"] += reserved
    state["cash_eur"] += proceeds - reserved

    trade = dict(pos)
    trade.update({
        "exit_price_eur": exit_price_eur,
        "exit_ts": now,
        "exit_reason": reason,
        "proceeds_eur": proceeds,
        "pnl_eur": pnl_eur,
        "pnl_pct": (pnl_eur / pos["cost_eur"] * 100) if pos["cost_eur"] else 0,
        "reserved_eur": reserved,
    })
    state["closed_trades"].append(trade)
    return trade

def _check_exits(state, eur_rate, now):
    actions = []
    if not state["positions"]:
        return actions

    ids = [p["id"] for p in state["positions"].values()]
    fresh = sources_coingecko.fetch_by_ids(ids)

    for key, pos in list(state["positions"].items()):
        c = fresh.get(pos["id"])
        if not c or not c.get("price_usd"):
            continue  # sem dados frescos nesta corrida — tenta de novo na próxima, não força saída

        price_eur = c["price_usd"] * eur_rate
        pos["last_price_eur"] = price_eur
        pos["peak_price_eur"] = max(pos.get("peak_price_eur", pos["entry_price_eur"]), price_eur)
        mcap = c.get("market_cap")
        if mcap:
            pos["last_market_cap"] = mcap

        peak = pos["peak_price_eur"]
        drawdown = (peak - price_eur) / peak if peak else 0
        if drawdown >= config.PUMP_WATCH_TRAILING_DRAWDOWN_PCT:
            trade = _close_position(
                state, key, price_eur,
                f"trailing stop hit — dropped {drawdown:.1%} from peak", now,
            )
            actions.append({"action": "sell", **trade})

    return actions

def run_pump_watch_cycle(cex_candidates, eur_rate):
    """Ciclo completo (screener principal, a cada 2h): reavalia/fecha posições, e só procura
    candidatos novos quando há slot livre. cex_candidates: lista bruta de candidatos CEX já
    descoberta nesta corrida (sources_coingecko.fetch_small_cap_candidates()), reutilizada sem
    custo extra de API. Devolve (state, actions)."""
    state = load_state()
    now = time.time()
    state["last_eur_rate"] = eur_rate

    exit_actions = _check_exits(state, eur_rate, now)
    entry_actions = _check_entries(state, cex_candidates, eur_rate, now)

    state["last_run_ts"] = now
    save_state(state)
    return state, exit_actions + entry_actions

def run_exit_check_cycle(eur_rate):
    """Ciclo leve (position_monitor.py, a cada poucos minutos): só reavalia/fecha posições já
    abertas — nunca procura candidatos novos (isso fica só para o ciclo principal, que já tem o
    universo CEX descoberto nesta mesma corrida). Devolve (state, actions)."""
    state = load_state()
    now = time.time()

    if not state["positions"]:
        return state, []

    state["last_eur_rate"] = eur_rate
    actions = _check_exits(state, eur_rate, now)
    state["last_run_ts"] = now
    save_state(state)
    return state, actions
