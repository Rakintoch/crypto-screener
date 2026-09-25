"""Catalisadores fundamentais por moeda, a partir de manchetes de notícias (sources_news.py).

Classifica as manchetes recentes em etiquetas (config.CATALYST_TAGS) e devolve:
  {"tags": [...], "score": 0..1, "blocking": bool, "headlines": [até 3], "latest_ts": ts}

- "score" soma o peso de cada etiqueta positiva distinta (queima/recompra de oferta,
  reestruturação/migração, listagem, upgrade, parceria), limitado a 1.
- "blocking" = há um incidente de segurança recente (hack, exploit, rug...) — o Pump Watch não
  entra nessas moedas.
- "exchange_risk" (ex: etiqueta de monitorização/risco de delisting da Binance) NÃO bloqueia: no
  caso LSK foi precisamente o que atraiu os shorts cuja liquidação alimentou o squeeze. Fica
  registado para a autoanálise decidir, com dados, se deve pesar a favor ou contra.

Tudo isto é registado em cada entrada (entry_catalyst_*) para a revisão de cada ciclo poder
medir se os catalisadores melhoram de facto o resultado — só depois disso deve ganhar peso.
"""
import re
import time
import traceback

from . import config
from . import sources_news


def classify(headlines):
    tags = set()
    matched = []
    for h in headlines:
        t = h["title"].lower()
        hit = False
        for tag, phrases in config.CATALYST_TAGS.items():
            for ph in phrases:
                if re.search(r"\b" + re.escape(ph) + r"\b", t):
                    tags.add(tag)
                    hit = True
                    break
        if hit:
            matched.append(h)
    score = sum(config.CATALYST_TAG_WEIGHTS.get(tag, 0.0) for tag in tags)
    score = max(0.0, min(1.0, score))
    return {
        "tags": sorted(tags),
        "score": score,
        "blocking": bool(tags & set(config.CATALYST_BLOCKING_TAGS)),
        "headlines": [{"title": h["title"][:160], "ts": h["ts"], "source": h.get("source")} for h in matched[:3]],
        "latest_ts": max((h["ts"] or 0 for h in matched), default=None) or None,
    }


def check(candidate, now=None):
    """Catalisadores de UMA moeda. Nunca levanta exceção nem bloqueia a corrida: se estiver
    desligado ou a fonte falhar, devolve None (= sem informação, não = sem catalisador)."""
    if not config.CATALYST_ENABLED:
        return None
    try:
        headlines = sources_news.fetch_headlines(candidate.get("name") or candidate.get("symbol"),
                                                 candidate.get("symbol"), now=now or time.time())
        return classify(headlines)
    except Exception:
        traceback.print_exc()
        return None


def entry_fields(cat):
    """Campos a guardar na posição/trade no momento da compra (para a autoanálise)."""
    if not cat:
        return {"entry_catalyst_tags": None, "entry_catalyst_score": None, "entry_catalyst_headlines": None}
    return {
        "entry_catalyst_tags": cat["tags"],
        "entry_catalyst_score": cat["score"],
        "entry_catalyst_headlines": cat["headlines"],
    }
