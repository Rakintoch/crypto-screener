"""
Camada 1: moedas 'small-cap' já listadas em exchanges (via CoinGecko, API pública, sem key).
Mais seguras/líquidas que a camada DEX, mas ainda fora do top da tabela — onde há mais espaço
para movimentos percentuais rápidos.

Também fornece fetch_by_ids(), usado pelo módulo de portfólio virtual para reavaliar o preço
e o score atual de posições já abertas, independentemente da página de ranking em que caem.
"""
from . import config
from .http_utils import get_json

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


def _parse_coin(coin):
    mcap = coin.get("market_cap") or 0
    vol = coin.get("total_volume") or 0
    turnover = (vol / mcap) if mcap else 0

    return {
        "tier": "cex_small_cap",
        "id": coin.get("id"),
        "symbol": (coin.get("symbol") or "").upper(),
        "name": coin.get("name"),
        "price_usd": coin.get("current_price"),
        "market_cap": mcap,
        "market_cap_rank": coin.get("market_cap_rank"),
        "volume_24h": vol,
        "turnover": turnover,
        "chg_1h": coin.get("price_change_percentage_1h_in_currency"),
        "chg_24h": coin.get("price_change_percentage_24h_in_currency"),
        "chg_7d": coin.get("price_change_percentage_7d_in_currency"),
        "url": f"https://www.coingecko.com/en/coins/{coin.get('id')}",
        "security": {"checked": False, "safe": True, "notes": "CEX listada — sem verificação on-chain aplicada"},
    }


def fetch_small_cap_candidates():
    candidates = []
    for page in range(config.COINGECKO_RANK_START_PAGE, config.COINGECKO_RANK_END_PAGE + 1):
        data = get_json(
            COINGECKO_MARKETS_URL,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": config.COINGECKO_PER_PAGE,
                "page": page,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
        )
        if not data:
            continue

        for coin in data:
            mcap = coin.get("market_cap") or 0
            vol = coin.get("total_volume") or 0
            if mcap < config.COINGECKO_MIN_MARKET_CAP or mcap > config.COINGECKO_MAX_MARKET_CAP:
                continue
            if vol < config.COINGECKO_MIN_VOLUME_USD:
                continue
            turnover = vol / mcap if mcap else 0
            if turnover < config.COINGECKO_MIN_TURNOVER:
                continue
            candidates.append(_parse_coin(coin))

    return candidates


def fetch_by_ids(ids):
    """
    Busca dados completos (preço + variações 1h/24h/7d) para uma lista específica de coin ids
    do CoinGecko — usado para reavaliar posições já abertas no portfólio virtual, sem depender
    de em que página de ranking o coin caiu nesta corrida.
    """
    ids = [i for i in ids if i]
    if not ids:
        return {}

    result = {}
    # a API aceita uma lista grande em "ids", mas dividimos em blocos por segurança
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = get_json(
            COINGECKO_MARKETS_URL,
            params={
                "vs_currency": "usd",
                "ids": ",".join(chunk),
                "per_page": 250,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
        )
        if not data:
            continue
        for coin in data:
            parsed = _parse_coin(coin)
            result[parsed["id"]] = parsed

    return result
