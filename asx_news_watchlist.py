import html
from difflib import SequenceMatcher
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


def title_tokens(title):
    title = html.unescape(title).casefold()
    title = re.sub(r"\b(\d+(?:\.\d+)?)\s*(?:million|m)\b", r" \1m ", title)
    title = re.sub(r"[^a-z0-9.]+", " ", title)
    ignored = {
        "the", "a", "an", "and", "for", "to", "of", "in", "on", "at", "by",
        "asx", "shares", "share", "company", "limited", "ltd", "announces",
        "update", "new", "market", "news", "stock", "stocks", "what", "you",
        "need", "know", "today", "media", "cleansing", "notice",
    }
    return {word for word in title.split() if word not in ignored and len(word) > 1}


def same_story(a, b):
    if a["ticker"] != b["ticker"]:
        return False
    ta, tb = title_tokens(a["title"]), title_tokens(b["title"])
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    sequence = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    nums_a = set(re.findall(r"\d+(?:\.\d+)?", a["title"]))
    nums_b = set(re.findall(r"\d+(?:\.\d+)?", b["title"]))
    same_figures = bool(nums_a & nums_b)
    return overlap >= 0.62 or sequence >= 0.70 or (same_figures and overlap >= 0.42)


def group_stories(items):
    groups = []
    for item in sorted(items, key=lambda x: x["timestamp"], reverse=True):
        match = next((group for group in groups if any(same_story(item, member) for member in group)), None)
        if match is None:
            groups.append([item])
        else:
            match.append(item)
    return groups


def publish(group, token):
    item = max(group, key=lambda x: x["timestamp"])
    ticker = html.escape(item["ticker"])
    company = html.escape(item["company"])
    title = html.escape(item["title"])
    date = datetime.fromisoformat(item["published"]).astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    description = html.escape(item.get("description", "")[:700])
    links = []
    seen_urls = set()
    for story in group:
        if story["url"] in seen_urls:
            continue
        seen_urls.add(story["url"])
        source = html.escape(story.get("source") or "Источник")
        url = html.escape(story["url"], quote=True)
        links.append(f'• <a href="{url}">{source}</a>')
    text = (
        f"<b>{title}</b>\n\n"
        f"<b>Компания:</b> {company} ({ticker})\n"
        f"<b>Дата:</b> {date}\n\n"
        f"{description}\n\n"
        f"<b>Материалы по этой новости ({len(links)}):</b>\n"
        + "\n".join(links[:8])
        + "\n\n#ASX #Новости"
    )
    telegram_api("sendMessage", token, {
        "chat_id": CHANNEL,
        "text": text[:4000],
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
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
    fresh_groups = group_stories(fresh)
    if first_run:
        selected = fresh_groups[:MAX_INITIAL_POSTS]
    else:
        selected = fresh_groups[:MAX_POSTS_PER_RUN]

    posted = 0
    for group in selected:
        publish(group, token)
        seen.update(item["key"] for item in group)
        posted += 1
        print(f"Опубликовано одной подборкой ({len(group)} ссылок): {group[0]['ticker']} — {group[0]['title']}")

    # Mark all collected links as seen so the feed doesn't replay a backlog.
    seen.update(item["key"] for item in ordered)
    STATE_FILE.write_text(
        json.dumps({"seen_keys": sorted(seen)[-MAX_SEEN:]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Компаний: {len(COMPANIES)}; найдено ссылок: {len(ordered)}; новых ссылок: {len(fresh)}; групп новостей: {len(fresh_groups)}; опубликовано подборок: {posted}")


if __name__ == "__main__":
    main()
