"""
Camada 2 (parte B): tokens em destaque no DexScreener (boosts pagos pelos próprios projetos —
não é sinal orgânico, mas correlaciona com atenção/volume de curto prazo, por isso entra
como sinal secundário, nunca como filtro principal).

fetch_market_data_for_addresses() é genérico e também é usado pelo módulo de portfólio
virtual para reavaliar posições DEX já abertas, dado um endereço concreto.
"""
import time

from .http_utils import get_json

BOOST_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
BOOST_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{addresses}"


def fetch_boosted_addresses():
    """Devolve o set de endereços (lowercase) que estão a pagar para aparecer em destaque."""
    addresses = set()
    for url in (BOOST_TOP_URL, BOOST_LATEST_URL):
        data = get_json(url)
        if not data:
            continue
        for entry in data:
            addr = entry.get("tokenAddress")
            if addr:
                addresses.add(addr.lower())
    return addresses


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_pair(addr, best, boosted):
    price_change = best.get("priceChange") or {}
    volume = best.get("volume") or {}
    liquidity = best.get("liquidity") or {}
    pair_created_ms = best.get("pairCreatedAt")

    age_minutes = None
    if pair_created_ms:
        age_minutes = (time.time() * 1000 - pair_created_ms) / 60000

    return {
        "tier": "dex_micro_cap",
        "id": addr,
        "network": best.get("chainId"),
        "symbol": (best.get("baseToken") or {}).get("symbol", "?"),
        "name": (best.get("baseToken") or {}).get("name"),
        "price_usd": _to_float(best.get("priceUsd")),
        "market_cap": _to_float(best.get("fdv") or best.get("marketCap")),
        "liquidity_usd": _to_float(liquidity.get("usd")),
        "volume_24h": _to_float(volume.get("h24")),
        "chg_1h": _to_float(price_change.get("h1")),
        "chg_6h": _to_float(price_change.get("h6")),
        "chg_24h": _to_float(price_change.get("h24")),
        "pool_age_minutes": age_minutes,
        "boosted": boosted,
        "url": best.get("url"),
        "pool_address": best.get("pairAddress"),
    }


def fetch_market_data_for_addresses(addresses, boosted_addresses=None, chain_filter=None, limit=60):
    """
    Busca dados de mercado reais (preço/volume/liquidez) para uma lista de endereços de token
    via /latest/dex/tokens/{address}. Usado tanto para os candidatos 'boosted' descobertos como
    para reavaliar posições DEX já abertas no portfólio virtual.
    """
    boosted_addresses = boosted_addresses or set()
    candidates = []

    for addr in list(addresses)[:limit]:
        data = get_json(TOKENS_URL.format(addresses=addr))
        if not data or not data.get("pairs"):
            continue

        pairs = data["pairs"]
        if chain_filter:
            pairs = [p for p in pairs if p.get("chainId") in chain_filter]
        if not pairs:
            continue

        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        boosted = addr.lower() in boosted_addresses
        candidates.append(_parse_pair(addr, best, boosted))

    return candidates


def fetch_market_data_for_boosted(boosted_addresses, chain_filter=None):
    """Mantido por compatibilidade — todos os endereços passados são, por definição, boosted."""
    return fetch_market_data_for_addresses(
        boosted_addresses, boosted_addresses=boosted_addresses, chain_filter=chain_filter
    )
