import html
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

CHANNEL = "-1001317033615"
STATE_FILE = Path("us_business_news_state.json")
MAX_POSTS_PER_RUN = 1
MAX_SEEN = 1200
LOOKBACK_DAYS = 3
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# Only sources selected for free-to-read reporting, official disclosures, or free corporate releases.
QUERIES = [
    'site:apnews.com/hub/business company earnings acquisition technology business',
    'site:apnews.com/article company earnings acquisition business',
    'site:globenewswire.com/news-release company earnings acquisition product launch',
    'site:stocktitan.net/news company earnings contract acquisition',
    'site:sec.gov/Archives/edgar/data company 8-K earnings acquisition',
    'site:businesswire.com/news/home company earnings acquisition product launch',
    'site:prnewswire.com/news-releases company earnings acquisition product launch',
    'US listed companies earnings guidance acquisition merger product launch corporate news',
    'US stocks company-specific news earnings revenue corporate deal',
]
ALLOWED_DOMAINS = (
    "apnews.com", "globenewswire.com", "stocktitan.net", "sec.gov",
    "businesswire.com", "prnewswire.com",
)
EXCLUDE_TERMS = (
    "inflation report", "consumer price index", "cpi report", "interest rate decision",
    "federal reserve rate", "fed rate decision", "gdp report", "unemployment rate",
    "jobs report", "treasury yields", "central bank", "monetary policy",
)


def request_json(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def google_news(query):
    after = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    params = urllib.parse.urlencode({
        "q": f"({query}) after:{after}",
        "hl": "en-US", "gl": "US", "ceid": "US:en",
    })
    req = urllib.request.Request(
        "https://news.google.com/rss/search?" + params,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BusinessNewsDigest/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=45) as response:
        root = ET.fromstring(response.read())
    items = []
    for node in root.findall("./channel/item"):
        title = html.unescape((node.findtext("title") or "").strip())
        link = (node.findtext("link") or "").strip()
        description = html.unescape(re.sub(r"<[^>]+>", " ", node.findtext("description") or ""))
        description = re.sub(r"\s+", " ", description).strip()
        source_node = node.find("source")
        source = (source_node.text or "").strip() if source_node is not None else "Source"
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
        if not title or not link or published < datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS):
            continue
        if any(term in (title + " " + description).casefold() for term in EXCLUDE_TERMS):
            continue
        # Resolve Google News redirect so the original publisher is linked in both places.
        try:
            redirect_req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(redirect_req, timeout=20) as response:
                original_url = response.geturl()
        except Exception:
            original_url = link
        host = urllib.parse.urlparse(original_url).netloc.lower().removeprefix("www.")
        source_name = source.casefold()
        if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS):
            if not any(name in source_name for name in (
                "ap news", "associated press", "globenewswire", "stock titan",
                "business wire", "pr newswire", "sec.gov",
            )):
                continue
        items.append({
            "title": title, "url": original_url, "description": description[:2500],
            "source": source, "published": published.isoformat(),
            "timestamp": published.timestamp(),
        })
    return items


def telegram(method, token, payload):
    result = request_json(
        f"https://api.telegram.org/bot{token}/{method}", payload,
        {"Content-Type": "application/json"},
    )
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result}")
    return result["result"]


def telegraph_api(method, payload):
    data = urllib.parse.urlencode({
        key: json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (list, dict)) else value
        for key, value in payload.items()
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegra.ph/{method}", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegraph {method} failed: {result}")
    return result["result"]


def get_telegraph_token():
    token = os.getenv("TELEGRAPH_ACCESS_TOKEN") or os.getenv("TELEGRAPH_TOKEN")
    if token:
        return token
    return telegraph_api("createAccount", {
        "short_name": "USBusinessNews", "author_name": "US Business News Digest",
    })["access_token"]


def write_english_brief(item):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        description = item.get("description") or "The publisher's feed provided no detailed preview."
        return description
    prompt = (
        "Create a useful, standalone English business-news brief based only on the supplied headline and "
        "feed excerpt. Write 3-5 concise paragraphs and a 'Key points' section with 2-4 bullets. Explain "
        "what happened, which company or companies are involved, and why it matters to the business or stock "
        "market reader. Include numbers only if supplied. Do not invent facts, quotes, financial impact, "
        "or predictions. If the excerpt is too thin, explicitly say the available source preview is limited. "
        "This must be an original summary, not a verbatim copy of copyrighted journalism.\n\n"
        f"Headline: {item['title']}\nPublisher: {item['source']}\n"
        f"Published: {item['published']}\nSource excerpt: {item.get('description', '')}"
    )
    response = request_json(
        "https://api.openai.com/v1/chat/completions",
        {"model": OPENAI_MODEL, "temperature": 0.2,
         "messages": [{"role": "user", "content": prompt}]},
        {"Authorization": f"Bearer {api_key}"},
    )
    return response["choices"][0]["message"]["content"].strip()


def format_telegraph_nodes(text):
    nodes = []
    lines = text.strip().splitlines()
    paragraph = []

    def flush_paragraph():
        if paragraph:
            nodes.append({"tag": "p", "children": [" ".join(paragraph)]})
            paragraph.clear()

    bullets = []
    def flush_bullets():
        if bullets:
            nodes.append({"tag": "ul", "children": [
                {"tag": "li", "children": [bullet]} for bullet in bullets
            ]})
            bullets.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_paragraph()
            flush_bullets()
        elif line.casefold().strip("#*: ") in {"key points", "what matters", "why it matters"}:
            flush_paragraph()
            flush_bullets()
            nodes.append({"tag": "h3", "children": [line.strip("#*: ")]})
        elif re.match(r"^[-•*]\s+", line):
            flush_paragraph()
            bullets.append(re.sub(r"^[-•*]\s+", "", line))
        else:
            flush_bullets()
            paragraph.append(line)
    flush_paragraph()
    flush_bullets()
    return nodes or [{"tag": "p", "children": ["No detailed source preview was available."]}]


def create_telegraph_page(token, item, brief):
    published = datetime.fromisoformat(item["published"]).strftime("%Y-%m-%d %H:%M UTC")
    content = [
        {"tag": "p", "children": [{"tag": "b", "children": ["Publisher: " + item["source"]]}]},
        {"tag": "p", "children": ["Published: " + published]},
        *format_telegraph_nodes(brief),
        {"tag": "hr"},
        {"tag": "p", "children": [
            {"tag": "a", "attrs": {"href": item["url"]}, "children": ["Read the original article"]}
        ]},
    ]
    result = telegraph_api("createPage", {
        "access_token": token, "title": item["title"][:256],
        "author_name": "US Business News Digest", "author_url": item["url"],
        "content": content, "return_content": False,
    })
    return result["url"]


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
            print(f"Search failed for {query[:70]!r}: {exc}")
    unique = {}
    for item in collected:
        unique.setdefault(item["url"], item)
    items = sorted(unique.values(), key=lambda x: x["timestamp"])
    fresh = [item for item in items if item["url"] not in seen]
    # On activation, sample only the newest result. Thereafter publish one per hourly run.
    if first_run and fresh:
        candidate = fresh[-1]
        seen.update(item["url"] for item in fresh[:-1])
    elif fresh:
        candidate = fresh[-1]
    else:
        candidate = None

    if candidate:
        brief = write_english_brief(candidate)
        page_url = create_telegraph_page(get_telegraph_token(), candidate, brief)
        source_url = html.escape(candidate["url"], quote=True)
        page_link = html.escape(page_url, quote=True)
        title = html.escape(candidate["title"])
        source = html.escape(candidate.get("source") or "Original source")
        date = datetime.fromisoformat(candidate["published"]).strftime("%Y-%m-%d %H:%M UTC")
        excerpt = html.escape((candidate.get("description") or "")[:450])
        message = (
            f"<b>{title}</b>\n\n{excerpt}\n\n"
            f"<b>Source:</b> {source}\n<b>Published:</b> {date}\n\n"
            f"<a href=\"{page_link}\">Read the full news brief in Telegram (Telegraph)</a>\n"
            f"<a href=\"{source_url}\">Original article</a>\n\n"
            "#USMarkets #Business #Companies"
        )
        telegram("sendMessage", bot_token, {
            "chat_id": CHANNEL, "text": message, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        })
        seen.add(candidate["url"])
        print(f"Published: {candidate['title']} | Telegraph: {page_url}")
    # Do not mark newer queued items as seen: they will be published on later runs.
    if candidate:
        seen.update(item["url"] for item in items if item["timestamp"] < candidate["timestamp"])
    STATE_FILE.write_text(
        json.dumps({"seen_urls": sorted(seen)[-MAX_SEEN:]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Unique free-source stories: {len(items)}; unseen: {len(fresh)}; published: {1 if candidate else 0}")


if __name__ == "__main__":
    main()
