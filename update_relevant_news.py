"""
Actualiza noticias relevantes de mercado y las guarda en base de datos.

Uso:
    python update_relevant_news.py

Variables utiles:
    NEWS_RSS_FEEDS=https://...rss,https://...rss
    NEWS_KEYWORDS=nasdaq,stocks,earnings,AI,semiconductors
    NEWS_LIMIT=25
    NEWS_AI_ENABLED=1
    OPENAI_API_KEY=...
    NEWS_AI_MODEL=...

La IA es opcional. Si no hay clave/modelo, se genera un resumen simple.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config_env import load_local_env
from db import engine


DEFAULT_FEEDS = [
    "https://finance.yahoo.com/news/rssindex",
    "https://www.investing.com/rss/news_25.rss",
]
DEFAULT_KEYWORDS = [
    "nasdaq",
    "stock",
    "stocks",
    "market",
    "earnings",
    "fed",
    "inflation",
    "rates",
    "ai",
    "semiconductor",
    "technology",
    "oil",
    "gold",
]


def main():
    load_local_env()
    ensure_market_news_table()
    feeds = configured_feeds()
    keywords = configured_keywords()
    limit = parse_int(os.environ.get("NEWS_LIMIT"), 25)

    print(f"Feeds configurados: {len(feeds)}")
    items = []
    for feed_url in feeds:
        try:
            feed_items = fetch_feed(feed_url)
        except Exception as error:
            print(f"No se pudo leer feed {feed_url}: {error}")
            continue
        print(f"{feed_url}: {len(feed_items)} noticias")
        items.extend(feed_items)

    filtered = filter_news(items, keywords, limit)
    print(f"Noticias relevantes filtradas: {len(filtered)}")

    saved = 0
    for item in filtered:
        enriched = enrich_news_item(item)
        if save_news_item(enriched):
            saved += 1

    pruned = prune_old_news()
    print(f"Noticias nuevas guardadas: {saved}")
    print(f"Noticias antiguas limpiadas: {pruned}")
    return 0


def configured_feeds():
    raw = os.environ.get("NEWS_RSS_FEEDS", "").strip()
    if not raw:
        return DEFAULT_FEEDS
    return [item.strip() for item in raw.split(",") if item.strip()]


def configured_keywords():
    raw = os.environ.get("NEWS_KEYWORDS", "").strip()
    if not raw:
        return DEFAULT_KEYWORDS
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def fetch_feed(feed_url):
    request = urllib.request.Request(
        feed_url,
        headers={
            "User-Agent": "Mozilla/5.0 TradingNewsBot/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw_xml = response.read()
    root = ET.fromstring(raw_xml)
    source = feed_source(root, feed_url)

    items = []
    for node in root.findall(".//item"):
        item = rss_item(node, source)
        if item["title"] and item["url"]:
            items.append(item)
    for node in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        item = atom_item(node, source)
        if item["title"] and item["url"]:
            items.append(item)
    return items


def feed_source(root, fallback):
    title = root.findtext(".//channel/title") or root.findtext(".//{http://www.w3.org/2005/Atom}title")
    if title:
        return clean_text(title)
    parsed = urllib.parse.urlparse(fallback)
    return parsed.netloc or fallback


def rss_item(node, source):
    title = clean_text(node.findtext("title"))
    description = clean_text(node.findtext("description"))
    link = clean_text(node.findtext("link"))
    published = parse_news_datetime(node.findtext("pubDate") or node.findtext("published"))
    return {
        "title": title,
        "description": description,
        "url": link,
        "source": source,
        "published_at": published,
    }


def atom_item(node, source):
    ns = "{http://www.w3.org/2005/Atom}"
    title = clean_text(node.findtext(f"{ns}title"))
    description = clean_text(node.findtext(f"{ns}summary") or node.findtext(f"{ns}content"))
    link = ""
    for link_node in node.findall(f"{ns}link"):
        href = link_node.attrib.get("href", "")
        if href:
            link = href
            break
    published = parse_news_datetime(node.findtext(f"{ns}published") or node.findtext(f"{ns}updated"))
    return {
        "title": title,
        "description": description,
        "url": link,
        "source": source,
        "published_at": published,
    }


def filter_news(items, keywords, limit):
    seen = set()
    scored = []
    for item in items:
        url = item["url"].strip()
        if not url or url in seen:
            continue
        seen.add(url)
        text_blob = f"{item['title']} {item['description']}".lower()
        score = sum(1 for keyword in keywords if keyword and keyword.lower() in text_blob)
        ticker_hits = extract_tickers(f"{item['title']} {item['description']}")
        if ticker_hits:
            score += min(5, len(ticker_hits))
        if score <= 0:
            continue
        scored.append((score, item))
    scored.sort(key=lambda entry: (entry[0], entry[1]["published_at"] or datetime.min), reverse=True)
    return [item for _score, item in scored[:limit]]


def enrich_news_item(item):
    text_blob = f"{item['title']}. {item['description']}"
    symbols = ", ".join(extract_tickers(text_blob))
    fallback_summary = simple_summary(text_blob)
    ai_result = ai_summary(item, fallback_summary)
    return {
        **item,
        "summary": ai_result.get("summary") or fallback_summary,
        "impact": ai_result.get("impact") or infer_impact(text_blob),
        "symbols": ai_result.get("symbols") or symbols,
        "sector_tags": ai_result.get("sector_tags") or infer_sector_tags(text_blob),
        "ai_used": 1 if ai_result.get("ai_used") else 0,
    }


def ai_summary(item, fallback_summary):
    if os.environ.get("NEWS_AI_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {"summary": fallback_summary, "ai_used": False}
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("NEWS_AI_MODEL", "").strip()
    if not api_key or not model:
        return {"summary": fallback_summary, "ai_used": False}

    prompt = (
        "Analiza esta noticia financiera para una web de trading. "
        "Responde solo JSON valido con estas claves exactas: summary, impact, symbols, sector_tags. "
        "summary: una sola frase corta, maximo 28 palabras, directo, sin relleno, sin mencionar la fuente salvo que sea imprescindible. "
        "impact: positivo, negativo o neutral. "
        "symbols: tickers afectados separados por coma; si no hay tickers claros, activos/indices/commodities afectados. "
        "sector_tags: sectores afectados separados por coma. "
        "No uses primera persona, no menciones IA, no digas 'no puedo', no des recomendacion de compra o venta.\n\n"
        f"Titulo: {item['title']}\n"
        f"Fuente: {item['source']}\n"
        f"Descripcion: {item['description']}"
    )
    payload = {
        "model": model,
        "input": prompt,
        "text": {"format": {"type": "json_object"}},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        parsed = extract_response_json(data)
        parsed["ai_used"] = True
        return parsed
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"IA omitida para noticia: {error}")
        return {"summary": fallback_summary, "ai_used": False}


def extract_response_json(data):
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text_value = content.get("text")
            if text_value:
                chunks.append(text_value)
    if not chunks and data.get("output_text"):
        chunks.append(data["output_text"])
    raw = "\n".join(chunks).strip()
    parsed = json.loads(raw)
    return {
        "summary": remove_ai_phrasing(parsed.get("summary", "")),
        "impact": normalize_impact(parsed.get("impact", "")),
        "symbols": normalize_list_text(parsed.get("symbols", "")),
        "sector_tags": normalize_list_text(parsed.get("sector_tags", "")),
    }


def save_news_item(item):
    with engine.begin() as connection:
        ensure_market_news_table(connection)
        exists = connection.execute(
            text("SELECT 1 FROM market_news WHERE url = :url LIMIT 1"),
            {"url": item["url"]},
        ).fetchone()
        if exists:
            connection.execute(
                text(
                    """
                    UPDATE market_news
                    SET title = :title,
                        source = :source,
                        published_at = :published_at,
                        summary = :summary,
                        impact = :impact,
                        symbols = :symbols,
                        sector_tags = :sector_tags,
                        ai_used = :ai_used,
                        created_at = :created_at
                    WHERE url = :url
                    """
                ),
                {
                    "title": item["title"][:500],
                    "source": item["source"][:160],
                    "url": item["url"][:1000],
                    "published_at": item["published_at"] or datetime.now(UTC).replace(tzinfo=None),
                    "summary": item["summary"][:1200],
                    "impact": normalize_impact(item["impact"]),
                    "symbols": item["symbols"][:300],
                    "sector_tags": item["sector_tags"][:300],
                    "ai_used": item["ai_used"],
                    "created_at": datetime.now(UTC).replace(tzinfo=None),
                },
            )
            return False
        connection.execute(
            text(
                """
                INSERT INTO market_news
                (title, source, url, published_at, summary, impact, symbols, sector_tags, ai_used, created_at)
                VALUES
                (:title, :source, :url, :published_at, :summary, :impact, :symbols, :sector_tags, :ai_used, :created_at)
                """
            ),
            {
                "title": item["title"][:500],
                "source": item["source"][:160],
                "url": item["url"][:1000],
                "published_at": item["published_at"] or datetime.now(UTC).replace(tzinfo=None),
                "summary": item["summary"][:1200],
                "impact": normalize_impact(item["impact"]),
                "symbols": item["symbols"][:300],
                "sector_tags": item["sector_tags"][:300],
                "ai_used": item["ai_used"],
                "created_at": datetime.now(UTC).replace(tzinfo=None),
            },
        )
    return True


def prune_old_news():
    retention_days = parse_int(os.environ.get("NEWS_RETENTION_DAYS"), 14)
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC).replace(tzinfo=None).timestamp() - retention_days * 86400
    cutoff_dt = datetime.fromtimestamp(cutoff)
    with engine.begin() as connection:
        result = connection.execute(
            text("DELETE FROM market_news WHERE created_at < :cutoff"),
            {"cutoff": cutoff_dt},
        )
    return result.rowcount or 0


def ensure_market_news_table(connection=None):
    id_column = "SERIAL PRIMARY KEY" if engine.dialect.name == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statement = text(
        f"""
        CREATE TABLE IF NOT EXISTS market_news (
            id {id_column},
            title TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL UNIQUE,
            published_at TIMESTAMP,
            summary TEXT NOT NULL DEFAULT '',
            impact TEXT NOT NULL DEFAULT 'neutral',
            symbols TEXT NOT NULL DEFAULT '',
            sector_tags TEXT NOT NULL DEFAULT '',
            ai_used INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    if connection is not None:
        connection.execute(statement)
        return
    with engine.begin() as managed_connection:
        managed_connection.execute(statement)


def parse_news_datetime(value):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def simple_summary(value, max_chars=170):
    text_value = clean_text(value)
    text_value = remove_ai_phrasing(text_value)
    sentence_match = re.search(r"^(.{20,170}?[.!?])(?:\s|$)", text_value)
    if sentence_match:
        return sentence_match.group(1).strip()
    if len(text_value) <= max_chars:
        return text_value
    return text_value[:max_chars].rsplit(" ", 1)[0] + "..."


def remove_ai_phrasing(value):
    blocked = [
        "como inteligencia artificial",
        "como ia",
        "no puedo",
        "no tengo acceso",
        "en resumen,",
    ]
    text_value = str(value or "")
    lowered = text_value.lower()
    for phrase in blocked:
        if phrase in lowered:
            text_value = re.sub(re.escape(phrase), "", text_value, flags=re.IGNORECASE)
            lowered = text_value.lower()
    return clean_text(text_value)


def normalize_list_text(value):
    if isinstance(value, list):
        parts = [clean_text(item) for item in value]
    else:
        parts = re.split(r"[,;|]", clean_text(value))
    unique = []
    for part in parts:
        part = part.strip()
        if not part or part.lower() in {"none", "n/a", "no aplica", "sin datos"}:
            continue
        if part not in unique:
            unique.append(part)
    return ", ".join(unique[:12])


def infer_impact(value):
    text_value = value.lower()
    negative = ["falls", "drop", "drops", "loss", "risk", "cuts", "warning", "selloff", "recession"]
    positive = ["rises", "gain", "gains", "beats", "growth", "surge", "record", "upgrade", "rally"]
    neg_score = sum(1 for word in negative if word in text_value)
    pos_score = sum(1 for word in positive if word in text_value)
    if pos_score > neg_score:
        return "positivo"
    if neg_score > pos_score:
        return "negativo"
    return "neutral"


def normalize_impact(value):
    value = str(value or "").strip().lower()
    if value in {"positivo", "positive"}:
        return "positivo"
    if value in {"negativo", "negative"}:
        return "negativo"
    return "neutral"


def infer_sector_tags(value):
    text_value = value.lower()
    tags = []
    mapping = {
        "Tecnologia": ["ai", "software", "chip", "semiconductor", "cloud", "technology"],
        "Energia": ["oil", "gas", "energy", "crude"],
        "Financiero": ["bank", "fed", "rates", "yield"],
        "Metales": ["gold", "silver", "copper", "metal"],
        "Salud": ["health", "pharma", "drug", "biotech"],
    }
    for tag, words in mapping.items():
        if any(word in text_value for word in words):
            tags.append(tag)
    return ", ".join(tags)


def extract_tickers(value):
    candidates = re.findall(r"\b[A-Z]{1,5}\b", str(value))
    blocked = {"THE", "AND", "FOR", "CEO", "CFO", "USA", "USD", "FED", "ETF", "IPO", "AI"}
    unique = []
    for candidate in candidates:
        if candidate in blocked or candidate in unique:
            continue
        unique.append(candidate)
    return unique[:12]


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLAlchemyError as error:
        print(f"No se pudieron actualizar noticias: {error}")
        raise SystemExit(1)
