"""Wrapper HTTP com retries/backoff simples, para não rebentar o workflow por causa de um 429/timeout pontual."""
import time
import requests
from . import config


def get_json(url, params=None, headers=None, max_retries=3, backoff_seconds=2):
    hdrs = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=hdrs, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 429:
                # rate limited — espera e tenta outra vez
                time.sleep(backoff_seconds * attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001 - queremos mesmo apanhar tudo aqui e continuar
            last_error = e
            time.sleep(backoff_seconds * attempt)

    print(f"[http_utils] falhou definitivamente: {url} -> {last_error}")
    return None
