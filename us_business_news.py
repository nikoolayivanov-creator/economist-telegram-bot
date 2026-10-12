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
STATE_FILE = Path("us_business_news_state.json")
MAX_POSTS_PER_RUN = 2
MAX_SEEN = 1200
LOOKBACK_DAYS = 3
FEEDS = [
    ("AP Business", 'site:apnews.com/hub/business OR site:apnews.com/article/business'),
    ("GlobeNewswire", 'site:globenewswire.com/news-release'),
    ("Federal Reserve", 'site:federalreserve.gov/newsevents'),
    ("Bureau of Labor Statistics", 'site:bls.gov/news.release'),
    ("SEC", 'site:sec.gov/Archives/edgar/data OR site:sec.gov/newsroom'),
    ("Stock Titan", 'site:stocktitan.net/news'),
]
QUERIES = [
    'US companies earnings outlook acquisition layoffs investment business',
    'US stock market companies corporate news earnings',
    'US economy inflation jobs Federal Reserve economic data',
]

def request_json(url, payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}: {exc.read().decode('utf-8', errors='replace')[:500]}") from exc

def google_news(query):
    params = urllib.parse.urlencode({
        "q": f"({query}) after:{(datetime.now(timezone.utc)-timedelta(days=LOOKBACK_DAYS)).date().isoformat()}",
        "hl": "en-US", "gl": "US", "ceid": "US:en",
    })
    url = "https://news.google.com/rss/search?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BusinessNewsDigest/1.0)"})
    with urllib.request.urlopen(req, timeout=45) as response:
        root = ET.fromstring(response.read())
    items = []
    for node in root.findall("./channel/item"):
        title = html.unescape((node.findtext("title") or "").strip())
        link = (node.findtext("link") or "").strip()
        desc = html.unescape(re.sub(r"<[^>]+>", " ", node.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        source_node = node.find("source")
        source = ((source_node.text or "").strip() if source_node is not None else "Source")
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
        if not title or not link or published < datetime.now(timezone.utc)-timedelta(days=LOOKBACK_DAYS):
            continue
        try:
            redirect_req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(redirect_req, timeout=20) as redirect_response:
                original_url = redirect_response.geturl()
        except Exception:
            original_url = link
        items.append({"title": title, "url": original_url, "description": desc[:1800], "source": source,
                      "published": published.isoformat(), "timestamp": published.timestamp()})
    return items

def telegram(method, token, payload):
    return request_json(f"https://api.telegram.org/bot{token}/{method}", payload,
                        {"Content-Type": "application/json"}).get("result")

def telegraph_token():
    token = os.getenv("TELEGRAPH_ACCESS_TOKEN") or os.getenv("TELEGRAPH_TOKEN")
    if token:
        return token
    # Fallback keeps first run usable; preferably set TELEGRAPH_ACCESS_TOKEN as a repository secret
    # so all pages are published under one stable Telegraph account.
    result = request_json("https://api.telegra.ph/createAccount", {
        "short_name": "USBusinessNews", "author_name": "US Business News Digest"
    })
    if not result.get("ok"):
        raise RuntimeError(f"Telegraph createAccount failed: {result}")
    return result["result"]["access_token"]

def create_telegraph_page(token, item):
    title = item["title"][:256]
    desc = item.get("description") or "Open the original source for the full report."
    # The Telegraph page is a readable English digest, not a verbatim copy of copyrighted journalism.
    content = [
        {"tag": "h3", "children": [title]},
        {"tag": "p", "children": ["Source: " + (item.get("source") or "Original publisher")]},
        {"tag": "p", "children": [desc[:1800]]},
        {"tag": "p", "children": ["This page is a news digest. Follow the original-source link below for the publisher's full article and context."]},
        {"tag": "p", "children": [{"tag": "a", "attrs": {"href": item["url"]}, "children": ["Read the original article"]}]},
        {"tag": "p", "children": ["Published: " + item["published"].replace("T", " ").replace("+00:00", " UTC")]},
    ]
    result = request_json("https://api.telegra.ph/createPage", {
        "access_token": token, "title": title, "author_name": item.get("source") or "News source",
        "content": content, "return_content": False
    })
    if not result.get("ok"):
        raise RuntimeError(f"Telegraph createPage failed: {result}")
    return result["result"]["url"]

def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    seen = set(state.get("seen_urls", []))
    first_run = not seen
    collected = []
    for query in QUERIES:
        try:
            collected.extend(google_news(query))
        except Exception as exc:
            print(f"Search failed for query {query!r}: {exc}")
    unique = {}
    for item in collected:
        # Keep only sources explicitly chosen as free-to-read or official release/document sources.
        host = urllib.parse.urlparse(item["url"]).netloc.lower().removeprefix("www.")
        source_name = (item.get("source") or "").lower()
        allowed = (
            any(domain in host for domain in (
                "apnews.com", "globenewswire.com", "federalreserve.gov", "bls.gov",
                "sec.gov", "stocktitan.net"
            ))
            or any(name in source_name for name in (
                "associated press", "ap news", "globenewswire", "federal reserve",
                "bureau of labor statistics", "sec", "stock titan"
            ))
        )
        if allowed:
            unique.setdefault(item["url"], item)
    items = sorted(unique.values(), key=lambda x: x["timestamp"], reverse=True)
    fresh = [item for item in items if item["url"] not in seen]
    # On the first run, publish only a small sample; later runs publish at most two new items.
    selected = fresh[:MAX_POSTS_PER_RUN]
    if first_run:
        selected = fresh[:2]
    token = telegraph_token() if selected else None
    posted = 0
    for item in selected:
        page_url = create_telegraph_page(token, item)
        source_url = html.escape(item["url"], quote=True)
        page_link = html.escape(page_url, quote=True)
        title = html.escape(item["title"])
        source = html.escape(item.get("source") or "Original source")
        date = datetime.fromisoformat(item["published"]).strftime("%Y-%m-%d %H:%M UTC")
        message = (
            f"<b>{title}</b>\n\n"
            f"<b>Source:</b> {source}\n"
            f"<b>Published:</b> {date}\n\n"
            f"<a href=\"{page_link}\">Read in Telegram (Telegraph)</a>\n"
            f"<a href=\"{source_url}\">Original article</a>\n\n"
            "#USMarkets #Business #Economy"
        )
        telegram("sendMessage", bot_token, {
            "chat_id": CHANNEL, "text": message, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True}
        })
        seen.add(item["url"])
        posted += 1
        print(f"Published: {item['title']} | {page_url}")
    # Mark discovered links as seen to avoid a backlog on subsequent runs.
    seen.update(item["url"] for item in items)
    STATE_FILE.write_text(json.dumps({"seen_urls": sorted(seen)[-MAX_SEEN:]}, indent=2) + "\n", encoding="utf-8")
    print(f"Sources matched: {len(items)}; fresh: {len(fresh)}; published: {posted}")

if __name__ == "__main__":
    main()
