"""Manchetes de notícias por moeda — fonte gratuita, sem API key (RSS de pesquisa do Google
News). Usado por catalysts.py para detetar CATALISADORES (queimas de oferta, migrações,
listagens, incidentes de segurança...) que os dados de preço/volume sozinhos não mostram.

Motivação (pedido do Ricardo 2026-09-25, caso LSK): a Lisk anunciou a 25/08 o fim da sua
blockchain e uma queima proposta de 100M LSK (-25% da oferta), e a 10/09 a migração obrigatória
— o preço ficou parado ~2 semanas antes de subir +1000%. Até aqui o bot só via preço/volume,
por isso esta informação nunca entrava na decisão nem ficava registada para a autoanálise.
"""
import email.utils
import re
import time
import xml.etree.ElementTree as ET

import requests

from . import config

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"


def _get_text(url, params=None, max_retries=2, backoff_seconds=2):
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT,
                                headers={"User-Agent": config.USER_AGENT})
            if resp.status_code == 429:
                time.sleep(backoff_seconds * attempt)
                continue
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001 - uma falha de notícias nunca pode travar o bot
            last_error = e
            time.sleep(backoff_seconds * attempt)
    print(f"[sources_news] falhou: {url} -> {last_error}")
    return None


def parse_rss(xml_text):
    """Devolve [{title, link, ts, source}] a partir de um RSS 2.0 (formato do Google News)."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        ts = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                ts = email.utils.parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                ts = None
        if title:
            items.append({"title": title, "link": link, "ts": ts, "source": source})
    return items


def _mentions_coin(title, name, symbol):
    """A manchete tem de nomear a moeda (nome completo ou símbolo como palavra inteira) — o
    Google News devolve muito ruído (páginas de preço, spam) numa pesquisa só pelo nome."""
    t = title.lower()
    if name and re.search(r"\b" + re.escape(name.lower()) + r"\b", t):
        return True
    if symbol and len(symbol) >= 3 and re.search(r"\b" + re.escape(symbol.lower()) + r"\b", t):
        return True
    return False


def fetch_headlines(name, symbol, lookback_days=None, now=None):
    """Manchetes recentes (últimos lookback_days) que mencionam a moeda. Nunca levanta exceção:
    em caso de falha devolve lista vazia."""
    lookback_days = lookback_days or config.CATALYST_LOOKBACK_DAYS
    now = now or time.time()
    if not name:
        return []
    query = f'"{name}" crypto when:{int(lookback_days)}d'
    text = _get_text(GOOGLE_NEWS_RSS_URL, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    if not text:
        return []
    cutoff = now - lookback_days * 86400
    out = []
    for it in parse_rss(text):
        if it["ts"] is not None and it["ts"] < cutoff:
            continue
        if not _mentions_coin(it["title"], name, symbol):
            continue
        out.append(it)
    out.sort(key=lambda it: it["ts"] or 0, reverse=True)
    return out
