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

from . import catalysts
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

    # "Salto de volume" (2026-09-25, ver config.PUMP_WATCH_MIN_VOLUME_SURGE): os volumes do
    # market_chart são volume-24h acumulado em cada hora, por isso compara-se o nível recente
    # (média das últimas 6 leituras) com o nível de base (mediana das primeiras 24).
    base = sorted(vols[:24])
    base_median = base[len(base) // 2] if base else 0
    recent = vols[-6:]
    recent_mean = sum(recent) / len(recent) if recent else 0
    volume_surge = (recent_mean / base_median) if base_median else 0.0

    return {
        "obv_score": obv / total_volume,
        "price_change_pct": (closes[-1] - price_start) / price_start,
        "volume_surge": volume_surge,
    }

def _scan_accumulation_candidates(cex_candidates, held_ids, slots_free):
    """Só chama a API de histórico para um shortlist pequeno (pré-filtrado por turnover, um
    critério já disponível sem custo extra) — nunca para todo o universo CEX, para respeitar o
    limite de chamadas partilhado da API pública do CoinGecko."""
    if slots_free <= 0:
        return []

    def _calm(c):
        # 2026-09-25 (caso LSK): só moedas cujo preço ainda NÃO fugiu — as que já estão a subir
        # ocupavam o top-10 por turnover e eram depois descartadas pela banda de preço, deixando
        # de fora as que estavam de facto a acumular. None (dado em falta) não exclui.
        chg_24h = c.get("chg_24h")
        chg_7d = c.get("chg_7d")
        if chg_24h is not None and chg_24h > config.PUMP_WATCH_CALM_MAX_CHG_24H_PCT:
            return False
        if chg_7d is not None and chg_7d > config.PUMP_WATCH_CALM_MAX_CHG_7D_PCT:
            return False
        return True

    pool = [c for c in cex_candidates if c.get("id") and c["id"] not in held_ids and c.get("price_usd") and _calm(c)]
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
        if signal["volume_surge"] < config.PUMP_WATCH_MIN_VOLUME_SURGE:
            continue
        # Catalisador fundamental (2026-09-25): só para quem já passou no sinal técnico — no
        # máximo PUMP_WATCH_SHORTLIST_SIZE chamadas por corrida. Um incidente de segurança
        # recente bloqueia; o resto só dá um pequeno bónus de ordenação e fica registado.
        cat = catalysts.check(c)
        if cat and cat["blocking"]:
            print(f"[pump_watch] {c.get('symbol')} bloqueada por catalisador negativo: {cat['tags']}")
            continue
        c["_pw_signal"] = signal
        c["_catalyst"] = cat
        rank = signal["obv_score"] + (config.PUMP_WATCH_CATALYST_BONUS * cat["score"] if cat else 0.0)
        scored.append((rank, c))

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

        # Pedido do Ricardo 2026-09-14 (caso SOXSB): o preço acima é uma média do CoinGecko
        # entre várias venues (exchanges e, por vezes, várias pools on-chain do mesmo
        # token), mas uma compra real só pode ser executada numa de cada vez. Uma chamada
        # extra por compra real (Pump Watch é sempre cex_small_cap — ver docstring do
        # módulo), nunca bloqueia a entrada se falhar (ver sources_coingecko.fetch_top_venue).
        try:
            entry_venue = sources_coingecko.fetch_top_venue(c["id"])
        except Exception:
            entry_venue = None

        # Endereço do contrato — pedido do Ricardo 2026-09-14, para mostrar na listagem de
        # posições abertas (ver portfolio.py, mesmo princípio: Pump Watch é sempre
        # cex_small_cap, ver docstring do módulo). Chamada independente da de entry_venue;
        # nunca bloqueia a entrada se falhar.
        try:
            entry_contract_address = sources_coingecko.fetch_contract_address(c["id"])
        except Exception:
            entry_contract_address = None

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
            # Venue mais líquida usada como referência do preço de entrada — pedido do
            # Ricardo 2026-09-14, ver comentário acima.
            "entry_venue": entry_venue,
            # Endereço do contrato on-chain — pedido do Ricardo 2026-09-14, ver comentário
            # acima. None para moedas nativas de uma chain própria ou se a chamada falhar.
            "entry_contract_address": entry_contract_address,
            # Snapshot do sinal e dos catalisadores na entrada (2026-09-25) — para a revisão de
            # cada ciclo poder medir o que de facto antecede os ganhos (ver catalysts.py).
            "entry_obv_score": (c.get("_pw_signal") or {}).get("obv_score"),
            "entry_price_change_3d": (c.get("_pw_signal") or {}).get("price_change_pct"),
            "entry_volume_surge": (c.get("_pw_signal") or {}).get("volume_surge"),
            "entry_chg_24h": c.get("chg_24h"),
            "entry_chg_7d": c.get("chg_7d"),
            "entry_turnover": c.get("turnover"),
            **catalysts.entry_fields(c.get("_catalyst")),
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

def _maybe_close_review_cycle(state, now):
    """Ciclos de revisão contínuos (pedido do Ricardo 2026-09-24, ver config.py junto de
    PUMP_WATCH_REVIEW_AFTER_*): quando o ciclo atual chega a PUMP_WATCH_REVIEW_AFTER_TRADES
    trades fechados OU PUMP_WATCH_REVIEW_AFTER_DAYS dias (o que vier primeiro), guarda o resumo
    em state["review_history"] e abre logo o ciclo seguinte — SEM mexer em saldo nem posições.
    Devolve o resumo (ou None se o ciclo ainda não fechou)."""
    if state.get("status") != "active" or not state.get("start_ts"):
        return None
    rc = state.get("review_cycle")
    if rc is None:
        rc = state["review_cycle"] = {
            "number": 1,
            "start_ts": state["start_ts"],
            "start_trade_index": 0,
            "start_total_eur": state["starting_balance_eur"],
        }
    trades = state["closed_trades"][rc["start_trade_index"]:]
    days = (now - rc["start_ts"]) / 86400
    if len(trades) < config.PUMP_WATCH_REVIEW_AFTER_TRADES and days < config.PUMP_WATCH_REVIEW_AFTER_DAYS:
        return None

    open_value = sum(p["qty"] * p.get("last_price_eur", p["entry_price_eur"]) for p in state["positions"].values())
    total = state["cash_eur"] + state["reserve_eur"] + open_value
    wins = [t for t in trades if t.get("pnl_eur", 0) > 0]
    summary = {
        "number": rc["number"],
        "start_ts": rc["start_ts"],
        "end_ts": now,
        "days": days,
        "trigger": "trades" if len(trades) >= config.PUMP_WATCH_REVIEW_AFTER_TRADES else "days",
        "num_trades": len(trades),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
        "avg_pnl_pct": (sum(t.get("pnl_pct", 0) for t in trades) / len(trades)) if trades else 0.0,
        "realized_pnl_eur": sum(t.get("pnl_eur", 0) for t in trades),
        "start_total_eur": rc["start_total_eur"],
        "end_total_eur": total,
        "open_positions_carried": [p["symbol"] for p in state["positions"].values()],
        "trades": [
            {"symbol": t.get("symbol"), "pnl_pct": t.get("pnl_pct"), "exit_reason": t.get("exit_reason"),
             "entry_ts": t.get("entry_ts"), "exit_ts": t.get("exit_ts")}
            for t in trades
        ],
    }
    state.setdefault("review_history", []).append(summary)
    state["review_cycle"] = {
        "number": rc["number"] + 1,
        "start_ts": now,
        "start_trade_index": len(state["closed_trades"]),
        "start_total_eur": total,
    }
    return summary


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
    review = _maybe_close_review_cycle(state, now)
    if review:
        # ação especial (não é compra/venda): main.py envia-a numa mensagem própria
        entry_actions.append({"action": "review_checkpoint", **review})

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
