"""Câmbio USD->EUR, para o portfólio virtual poder trabalhar em EUR (o plafão do Ricardo é
em euros) apesar de a maioria das fontes de dados devolver preços em USD."""
from .http_utils import get_json

EXCHANGE_RATES_URL = "https://api.coingecko.com/api/v3/exchange_rates"

# usado apenas se a API falhar — mantém o sistema a funcionar em vez de rebentar
FALLBACK_USD_TO_EUR = 0.92


def get_usd_to_eur_rate():
    data = get_json(EXCHANGE_RATES_URL)
    try:
        rates = data["rates"]
        usd_per_btc = rates["usd"]["value"]
        eur_per_btc = rates["eur"]["value"]
        if usd_per_btc:
            return eur_per_btc / usd_per_btc
    except Exception:
        pass
    print(f"[fx] não foi possível obter taxa USD->EUR em tempo real, a usar fallback {FALLBACK_USD_TO_EUR}")
    return FALLBACK_USD_TO_EUR
