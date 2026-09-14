"""
Camada 2 (parte A): pools DEX em tendência/novas, via GeckoTerminal (API pública do próprio
CoinGecko para dados on-chain, sem key). Aqui é que aparecem os movimentos mais rápidos e
mais arriscados — tokens muito pequenos, recém-criados.
"""
import time
from datetime import datetime, timezone

from . import config
from .http_utils import get_json

BASE_URL = "https://api.geckoterminal.com/api/v2"

# Pedido do Ricardo 2026-09-14: os links partilhados nos alertas devem apontar para o
# DexScreener, não para o GeckoTerminal — a GeckoTerminal usa os seus próprios slugs de rede
# ("eth" para Ethereum mainnet), que nem sempre coincidem com os slugs que o DexScreener espera
# no URL (ex: "ethereum"). Este mapeamento cobre as redes em GECKOTERMINAL_NETWORKS (config.py);
# uma rede sem entrada aqui usa o próprio slug do GeckoTerminal como fallback.
DEXSCREENER_CHAIN_SLUGS = {
    "solana": "solana",
    "base": "base",
    "eth": "ethereum",
    "bsc": "bsc",
}


def _parse_pool(entry, boosted_addresses):
    attrs = entry.get("attributes", {}) or {}
    rels = entry.get("relationships", {}) or {}

    base_token_ref = (((rels.get("base_token") or {}).get("data") or {}).get("id")) or ""
    # formato tipicamente "<network>_<address>"
    network, _, address = base_token_ref.partition("_")
    if not address:
        return None

    price_change = attrs.get("price_change_percentage") or {}
    volume = attrs.get("volume_usd") or {}
    liquidity_usd = attrs.get("reserve_in_usd")
    fdv_usd = attrs.get("fdv_usd") or attrs.get("market_cap_usd")
    created_at = attrs.get("pool_created_at")

    age_minutes = None
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - created_dt).total_seconds() / 60
        except Exception:
            age_minutes = None

    def _to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    pool_address = attrs.get("address", "")
    dex_chain = DEXSCREENER_CHAIN_SLUGS.get(network, network)

    return {
        "tier": "dex_micro_cap",
        "id": address,
        "network": network,
        "symbol": (attrs.get("name") or "?").split("/")[0].strip(),
        "name": attrs.get("name"),
        "price_usd": _to_float(attrs.get("base_token_price_usd")),
        "market_cap": _to_float(fdv_usd),
        "liquidity_usd": _to_float(liquidity_usd),
        "volume_24h": _to_float(volume.get("h24")),
        "chg_1h": _to_float(price_change.get("h1")),
        "chg_6h": _to_float(price_change.get("h6")),
        "chg_24h": _to_float(price_change.get("h24")),
        "pool_age_minutes": age_minutes,
        "boosted": address.lower() in boosted_addresses,
        "url": f"https://dexscreener.com/{dex_chain}/{pool_address}",
        "pool_address": pool_address,
    }


def fetch_trending_and_new_pools(boosted_addresses=None):
    boosted_addresses = boosted_addresses or set()
    candidates = []

    for network in config.GECKOTERMINAL_NETWORKS:
        for endpoint in ("trending_pools", "new_pools"):
            data = get_json(f"{BASE_URL}/networks/{network}/{endpoint}", params={"page": 1})
            if not data:
                continue
            for entry in data.get("data", []) or []:
                parsed = _parse_pool(entry, boosted_addresses)
                if parsed:
                    candidates.append(parsed)
            time.sleep(1)  # ser simpático com o rate limit gratuito

    return candidates
