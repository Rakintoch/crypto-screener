"""
Gate eliminatório de segurança para tokens DEX (camada micro-cap), via GoPlus Security API
(gratuita, sem key). Um token que falhe estes critérios NUNCA é alertado, independentemente
do score de momentum — o objetivo é evitar honeypots e rugs óbvios, não garantir lucro.

Isto não substitui a tua própria due diligence. É um filtro de "risco óbvio", não um selo de
confiança.
"""
from . import config
from .http_utils import get_json

EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
SOLANA_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"


def _evaluate_evm(result):
    if not result:
        return {"checked": False, "safe": True, "notes": "no GoPlus data — not blocked, but not confirmed"}

    flags = []
    if result.get("is_honeypot") == "1":
        flags.append("honeypot")
    if result.get("cannot_sell_all") == "1":
        flags.append("cannot sell all")
    if result.get("is_blacklisted") == "1":
        flags.append("contract blacklisted")
    if result.get("transfer_pausable") == "1":
        flags.append("transfers pausable by owner")
    if result.get("is_mintable") == "1" and result.get("owner_address") not in ("", None, "0x0000000000000000000000000000000000000000"):
        flags.append("mintable + active owner")

    buy_tax = float(result.get("buy_tax") or 0)
    sell_tax = float(result.get("sell_tax") or 0)
    if buy_tax > 0.15 or sell_tax > 0.15:
        flags.append(f"high taxes (buy {buy_tax:.0%}/sell {sell_tax:.0%})")

    safe = len(flags) == 0
    return {"checked": True, "safe": safe, "notes": "; ".join(flags) if flags else "no obvious red flags"}


def _evaluate_solana(result):
    if not result:
        return {"checked": False, "safe": True, "notes": "no GoPlus data — not blocked, but not confirmed"}

    flags = []
    if result.get("freezable", {}).get("status") == "1":
        flags.append("mint has active freeze authority")
    if result.get("mintable", {}).get("status") == "1":
        flags.append("active mint authority (supply can be inflated)")
    if result.get("transfer_fee_upgradable", {}).get("status") == "1":
        flags.append("transfer fee is upgradable")
    non_transferable = result.get("non_transferable")
    if non_transferable == "1":
        flags.append("non-transferable token")

    safe = len(flags) == 0
    return {"checked": True, "safe": safe, "notes": "; ".join(flags) if flags else "no obvious red flags"}


def check_token_security(network, address):
    """
    Devolve {"checked": bool, "safe": bool, "notes": str}.
    'safe=True' com 'checked=False' significa "não conseguimos verificar" — tratado como
    neutro, não como aprovação; a camada de scoring deve penalizar isto ligeiramente.
    """
    try:
        if network == "solana":
            data = get_json(SOLANA_URL, params={"contract_addresses": address})
            result = ((data or {}).get("result") or {}).get(address.lower()) or ((data or {}).get("result") or {}).get(address)
            return _evaluate_solana(result or {})

        chain_id = config.GOPLUS_EVM_CHAIN_IDS.get(network)
        if not chain_id:
            return {"checked": False, "safe": True, "notes": f"network '{network}' has no GoPlus support configured"}

        data = get_json(EVM_URL.format(chain_id=chain_id), params={"contract_addresses": address})
        result = ((data or {}).get("result") or {}).get(address.lower())
        return _evaluate_evm(result or {})

    except Exception as e:  # noqa: BLE001
        return {"checked": False, "safe": True, "notes": f"error checking security: {e}"}
