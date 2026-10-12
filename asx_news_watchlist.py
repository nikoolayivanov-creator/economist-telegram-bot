import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

CHANNEL = "-1001317033615"
STATE_FILE = Path("asx_news_state.json")
MAX_INITIAL_POSTS = 5
MAX_POSTS_PER_RUN = 1
MAX_SEEN = 1500
LOOKBACK_DAYS = 7
COMPANIES = [
    ("MEI.AX", "Meteoric Resources", '"Meteoric Resources" OR "MEI ASX"'),
    ("OD6.AX", "OD6 Metals", '"OD6 Metals" OR "OD6 ASX"'),
    ("LMG.AX", "Latrobe Magnesium", '"Latrobe Magnesium" OR "LMG ASX"'),
    ("BRN.AX", "BrainChip Holdings", '"BrainChip" OR "BRN ASX"'),
    ("4DS.AX", "4DS Memory", '"4DS Memory" OR "4DS ASX"'),
]


def fetch_google_news(query):
    params = urllib.parse.urlencode({
        "q": f"({query}) after:{(datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date().isoformat()}",
        "hl": "en-AU",
        "gl": "AU",
        "ceid": "AU:en",
    })
    url = f"https://news.google.com/rss/search?{params}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; ASXNewsDigest/1.0)"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        root = ET.fromstring(response.read())
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = item.findtext("description") or ""
        pub_date = item.findtext("pubDate") or ""
        source_node = item.find("source")
        source = ((source_node.text or "").strip() if source_node is not None else "")
        try:
            published = parsedate_to_datetime(pub_date)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
        if not title or not link or published < datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS):
            continue
        snippet = html.unescape(re.sub(r"<[^>]+>", " ", description))
        snippet = re.sub(r"\s+", " ", snippet).strip()
        items.append({
            "title": html.unescape(title),
            "url": link,
            "description": snippet[:900],
            "source": source,
            "published": published.isoformat(),
            "timestamp": published.timestamp(),
        })
    return items


def telegram_api(method, token, payload):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API {method} HTTP {error.code}: {details}") from error
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {result}")
    return result["result"]


def publish(item, token):
    ticker = html.escape(item["ticker"])
    company = html.escape(item["company"])
    title = html.escape(item["title"])
    source = html.escape(item.get("source") or "Google News")
    date = datetime.fromisoformat(item["published"]).astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    description = html.escape(item.get("description", "")[:900])
    url = html.escape(item["url"], quote=True)
    text = (
        f"<b>{title}</b>\n\n"
        f"<b>Компания:</b> {company} ({ticker})\n"
        f"<b>Дата:</b> {date}\n"
        f"<b>Источник:</b> {source}\n\n"
        f"{description}\n\n"
        f"<a href=\"{url}\">Открыть новость</a>\n\n"
        "#ASX #Новости"
    )
    telegram_api("sendMessage", token, {
        "chat_id": CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": False},
    })


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    seen = set(state.get("seen_keys", []))
    first_run = not seen

    collected = []
    for ticker, company, query in COMPANIES:
        for item in fetch_google_news(query):
            item["ticker"] = ticker
            item["company"] = company
            key = item["url"] or (item["ticker"] + "|" + item["title"].casefold())
            item["key"] = key
            collected.append(item)

    unique = {}
    for item in collected:
        unique.setdefault(item["key"], item)
    ordered = sorted(unique.values(), key=lambda x: x["timestamp"])
    fresh = [item for item in ordered if item["key"] not in seen]
    if first_run:
        selected = fresh[-MAX_INITIAL_POSTS:]
    else:
        selected = fresh[:MAX_POSTS_PER_RUN]

    posted = 0
    for item in selected:
        publish(item, token)
        seen.add(item["key"])
        posted += 1
        print(f"Опубликовано: {item['ticker']} — {item['title']}")

    # Mark all collected links as seen so the feed doesn't replay a backlog.
    seen.update(item["key"] for item in ordered)
    STATE_FILE.write_text(
        json.dumps({"seen_keys": sorted(seen)[-MAX_SEEN:]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Источников-компаний: {len(COMPANIES)}; найдено новостей: {len(ordered)}; новых: {len(fresh)}; опубликовано: {posted}")


if __name__ == "__main__":
    main()
