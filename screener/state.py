"""
Estado persistente entre corridas (ficheiro data/state.json, commitado de volta ao repo
pelo próprio workflow do GitHub Actions). Evita alertar o mesmo token repetidamente a
cada corrida, a não ser que o score tenha acelerado significativamente.
"""
import json
import os
import time

from . import config


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return {}
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _cleanup(state):
    cutoff = time.time() - config.STATE_MAX_AGE_HOURS * 3600
    return {k: v for k, v in state.items() if v.get("last_alert_ts", 0) >= cutoff}


def filter_new_or_accelerating(candidates, state):
    """Devolve apenas os candidatos que valem um alerta agora, e atualiza o estado in-place."""
    state = _cleanup(state)
    now = time.time()
    to_alert = []

    for c in candidates:
        key = f"{c['tier']}:{c['id']}"
        prev = state.get(key)

        if prev is None:
            to_alert.append(c)
        else:
            hours_since = (now - prev.get("last_alert_ts", 0)) / 3600
            score_increase = c["score"] - prev.get("last_score", 0)
            if hours_since >= config.ALERT_COOLDOWN_HOURS or score_increase >= config.RE_ALERT_MIN_SCORE_INCREASE:
                to_alert.append(c)

    for c in to_alert:
        key = f"{c['tier']}:{c['id']}"
        state[key] = {"last_alert_ts": now, "last_score": c["score"], "symbol": c.get("symbol")}

    return to_alert, state
