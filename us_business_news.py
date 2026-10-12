import html
from html.parser import HTMLParser
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


class ArticleTextParser(HTMLParser):
    """Extract readable paragraphs while ignoring page chrome and scripts."""
    SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs = []
        self.current = []
        self.skip_stack = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip_stack.append(tag)
            return
        if self.skip_stack:
            return
        if tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self._flush()

    def handle_endtag(self, tag):
        if self.skip_stack:
            if tag == self.skip_stack[-1] or tag in self.SKIP_TAGS:
                # HTML is imperfect; unwind through the matching skipped tag.
                if tag in self.skip_stack:
                    while self.skip_stack:
                        popped = self.skip_stack.pop()
                        if popped == tag:
                            break
            return
        if tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self._flush()

    def handle_data(self, data):
        if self.skip_stack:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if cleaned:
            self.current.append(cleaned)

    def _flush(self):
        text = re.sub(r"\s+", " ", " ".join(self.current)).strip()
        if len(text) > 25:
            self.paragraphs.append(text)
        self.current = []

    def close(self):
        super().close()
        self._flush()


def fetch_article_text(url):
    """Try the publisher directly, then Jina Reader's public text view."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,text/plain",
        "Accept-Language": "en-US,en;q=0.9",
    }
    attempts = [
        ("publisher", url, True),
        ("reader", "https://r.jina.ai/" + url, False),
    ]
    for label, target, parse_html in attempts:
        try:
            req = urllib.request.Request(target, headers=headers)
            with urllib.request.urlopen(req, timeout=35) as response:
                raw = response.read(2_000_000).decode("utf-8", errors="replace")
            if parse_html:
                parser = ArticleTextParser()
                parser.feed(raw)
                parser.close()
                paragraphs = parser.paragraphs
            else:
                # Jina Reader returns readable plain text, often including metadata first.
                paragraphs = [
                    re.sub(r"^#+\\s*", "", line).strip()
                    for line in raw.splitlines()
                    if len(re.sub(r"^#+\\s*", "", line).strip()) > 35
                ]
                paragraphs = [
                    p for p in paragraphs
                    if not p.casefold().startswith(("title:", "url source:", "markdown content:"))
                ]
            clean = []
            for paragraph in paragraphs:
                normalized = paragraph.casefold()
                if any(term in normalized for term in (
                    "subscribe to our newsletter", "sign up for our newsletter",
                    "privacy policy", "terms of use", "all rights reserved",
                    "cookie policy", "read more stories",
                )):
                    continue
                if paragraph not in clean:
                    clean.append(paragraph)
            text = "\\n\\n".join(clean)[:16000]
            if len(re.sub(r"\\s+", "", text)) >= 500:
                print(f"Retrieved article text via {label}: {len(text)} characters.")
                return text
            print(f"{label} returned too little article text ({len(text)} characters).")
        except Exception as exc:
            print(f"{label} fetch failed: {type(exc).__name__}: {exc}")
    return ""


def write_english_brief(item):
    # No paid API required. Prefer the publisher's readable page; if blocked,
    # use the full RSS preview only when it contains enough substance to stand alone.
    article_text = fetch_article_text(item["url"])
    excerpt = re.sub(r"\s+", " ", item.get("description", "")).strip()
    title = item["title"].strip()

    source_text = article_text if len(re.sub(r"\s+", "", article_text)) >= 500 else ""
    if not source_text and len(excerpt) >= 260:
        source_text = excerpt
        print("Using substantial publisher RSS preview; article HTML was unavailable.")
    if len(re.sub(r"\s+", "", source_text)) < 260:
        raise RuntimeError("Publisher page and RSS preview contain too little readable text.")

    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    normalized_source = re.sub(r"[^a-z0-9]+", " ", source_text.casefold()).strip()
    if normalized_source == normalized_title or len(normalized_source) < 260:
        raise RuntimeError("Source text is not substantial enough for a useful brief.")

    # Preserve publisher paragraph boundaries where available. For RSS previews,
    # make readable paragraphs from the supplied text without adding unsupported claims.
    paragraphs_in = [p.strip() for p in source_text.splitlines() if len(p.strip()) > 35]
    if len(paragraphs_in) < 2:
        sentences = re.split(r"(?<=[.!?])\s+", source_text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 35]
        paragraphs_in = [" ".join(sentences[i:i + 2]) for i in range(0, len(sentences), 2)]

    paragraphs = [
        "This brief summarizes information available from the publisher. It does not add unverified claims or investment advice."
    ]
    paragraphs.extend(paragraphs_in[:8])
    paragraphs.append("Read the original publisher article for full details; the publisher page may contain additional context.")
    return "\n\n".join(paragraphs)


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
    skipped = set(state.get("skipped_urls", []))
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
    fresh = [
        item for item in items
        if item["url"] not in seen and item["url"] not in skipped
    ]
    # On an empty state file, start with the newest available story; after that,
    # work oldest-first so queued stories are not silently jumped over.
    if not seen and not skipped:
        fresh.reverse()

    published_count = 0
    # Try multiple candidate stories in one run. A page with insufficient text is
    # skipped without aborting the workflow, so one blocked publisher cannot stall the feed.
    for candidate in fresh:
        if published_count >= MAX_POSTS_PER_RUN:
            break
        try:
            brief = write_english_brief(candidate)
            normalized_title = re.sub(r"[^a-z0-9]+", " ", candidate["title"].casefold()).strip()
            normalized_brief = re.sub(r"[^a-z0-9]+", " ", brief.casefold()).strip()
            if len(normalized_brief) < 280 or normalized_brief == normalized_title:
                raise RuntimeError("Brief is too short; skipped publication.")

            telegraph_token = get_telegraph_token()
            page_url = create_telegraph_page(telegraph_token, candidate, brief)
            source_url = html.escape(candidate["url"], quote=True)
            page_link = html.escape(page_url, quote=True)
            title = html.escape(candidate["title"])
            source = html.escape(candidate.get("source") or "Original source")
            date = datetime.fromisoformat(candidate["published"]).strftime("%Y-%m-%d %H:%M UTC")
            message = (
                f"<b>{title}</b>\n\n"
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
            published_count += 1
            print(f"Published: {candidate['title']} | Telegraph: {page_url}")
        except Exception as exc:
            # Keep going to the next source. Remember permanently unusable stories
            # separately from successfully published ones to avoid retrying them hourly.
            skipped.add(candidate["url"])
            print(f"Skipping story {candidate.get('title', '(untitled)')!r}: {type(exc).__name__}: {exc}")
            continue

    STATE_FILE.write_text(
        json.dumps({
            "seen_urls": sorted(seen)[-MAX_SEEN:],
            "skipped_urls": sorted(skipped)[-MAX_SEEN:],
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Unique free-source stories: {len(items)}; "
        f"unseen/unskipped: {len(fresh)}; published: {published_count}"
    )


if __name__ == "__main__":
    main()
