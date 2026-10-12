import html
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from bs4 import BeautifulSoup, NavigableString, Tag
from pathlib import Path
from email.utils import parsedate_to_datetime

RSS_URL = "https://habr.com/ru/rss/articles/?fl=ru&with_hubs=true&with_tags=true&limit=100"
STATE_FILE = Path("habr_state.json")
CHANNEL = "-1001317033615"
MAX_INITIAL_POSTS = 5
MAX_POSTS_PER_RUN = 1
MAX_SEEN_URLS = 1000

AI_TERMS = [
    "chatgpt", "chat gpt", "gpt-4", "gpt-5", "gpt-4o", "openai",
    "claude", "anthropic", "сонет claude", "опус claude",
]
ECONOMY_TERMS = [
    "экономик", "экономическ", "инфляц", "центробанк", "центральный банк",
    "ключевая ставка", "процентная ставка", "ставка фрс", "фрс",
    "ввп", "государственный долг", "госдолг", "облигац", "инвестиц",
    "финансов", "финансирован", "фондовый рынок", "биржа", "акци",
    "валют", "рубл", "доллар", "нефт", "золот", "налог", "бюджет",
    "банковск", "кредит", "ипотек", "рецесси", "макроэконом",
    "stock market", "interest rate", "central bank", "inflation", "gdp",
    "economy", "economic", "finance", "financial", "investment",
    "investing", "bond market", "equities", "recession", "federal reserve",
    "fed rate", "monetary policy", "fiscal policy", "earnings",
]
COMPANY_TERMS = [
    "компан", "корпорац", "стартап", "бизнес", "предпринимател", "выручк",
    "прибыл", "убыт", "сделка", "слияни", "поглощени", "ipo",
    "акционер", "гендиректор", "ceo", "cfo", "рынок компании",
    "company", "companies", "corporate", "startup", "business", "revenue",
    "profit", "merger", "acquisition", "shareholder", "earnings",
    "microsoft", "google", "alphabet", "apple", "amazon", "meta", "nvidia",
    "tesla", "samsung", "intel", "amd", "openai", "anthropic", "yandex",
    "яндекс", "сбер", "тинькофф", "втб", "газпром", "лукойл", "роснефть",
]


def get_text(element, name):
    node = element.find(name)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def clean_html(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def classify(title, description, categories):
    title_text = title.casefold()
    body_text = (description + " " + categories).casefold()
    labels = []

    ai_title = any(term in title_text for term in AI_TERMS)
    ai_body = any(term in body_text for term in AI_TERMS)
    econ_title = any(term in title_text for term in ECONOMY_TERMS)
    econ_body = any(term in body_text for term in ECONOMY_TERMS)
    company_title = any(term in title_text for term in COMPANY_TERMS)
    company_body = any(term in body_text for term in COMPANY_TERMS)

    if ai_title or ai_body:
        labels.append("Claude / ChatGPT / ИИ")
    # Economic coverage is limited to company/business stories, not macroeconomics.
    if (econ_title or econ_body) and (company_title or company_body):
        labels.append("Компании и бизнес")

    return labels


def fetch_articles():
    request = urllib.request.Request(
        RSS_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; HabrTelegramBot/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("В RSS Habr не найден channel")

    articles = []
    for item in channel.findall("item"):
        title = get_text(item, "title")
        url = get_text(item, "link")
        if not title or not url:
            continue

        description = clean_html(get_text(item, "description"))
        author = ""
        for node in item.iter():
            local_name = node.tag.rsplit("}", 1)[-1].casefold()
            if local_name in {"creator", "author"} and node.text and node.text.strip():
                author = node.text.strip()
                break
        categories = " ".join(
            (node.text or "").strip()
            for node in item.findall("category")
        )
        # Some Habr RSS feeds expose hubs and tags as namespaced elements.
        for node in item.iter():
            local_name = node.tag.rsplit("}", 1)[-1].casefold()
            if local_name in {"hub", "tag"} and node.text:
                categories += " " + node.text.strip()

        labels = classify(title, description, categories)
        pub_date = get_text(item, "pubDate")
        try:
            timestamp = parsedate_to_datetime(pub_date).timestamp()
        except (TypeError, ValueError, OverflowError):
            timestamp = 0

        articles.append({
            "title": title,
            "url": url,
            "description": description,
            "author": author,
            "labels": labels,
            "timestamp": timestamp,
        })

    if not articles:
        raise RuntimeError("RSS Habr получен, но статей в нём нет")
    return sorted(articles, key=lambda article: article["timestamp"])


def telegram_api(method, token, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Telegram API {method}: HTTP {error.code}: {details}"
        ) from error
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {result}")
    return result["result"]


def verify_channel(token):
    bot = telegram_api("getMe", token)
    print(f"Telegram token accepted; bot username: @{bot.get('username', 'unknown')}")
    chat = telegram_api("getChat", token, {"chat_id": CHANNEL})
    print(
        f"Target resolved: title={chat.get('title')!r}, "
        f"username={chat.get('username')!r}, type={chat.get('type')!r}, "
        f"id={chat.get('id')}"
    )
    return chat


def telegraph_api(method, payload):
    body = urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegra.ph/{method}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegraph {method} HTTP {error.code}: {detail}") from error
    if not result.get("ok"):
        raise RuntimeError(f"Telegraph {method} failed: {result}")
    return result["result"]


def telegraph_nodes(element):
    allowed = {"p", "h3", "h4", "pre", "blockquote", "ul", "ol", "li",
               "b", "strong", "i", "em", "a", "code", "br", "hr"}
    nodes = []
    for child in element.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text.strip():
                nodes.append(text)
            continue
        if not isinstance(child, Tag):
            continue
        tag = child.name.lower()
        if tag in {"script", "style", "noscript", "button", "svg", "figure"}:
            continue
        if tag in {"div", "section", "header", "footer", "span", "article"}:
            nodes.extend(telegraph_nodes(child))
            continue
        if tag == "img":
            src = child.get("src") or child.get("data-src")
            if src and src.startswith("https://"):
                nodes.append({"tag": "img", "attrs": {"src": src}})
            continue
        converted = telegraph_nodes(child)
        if tag not in allowed:
            nodes.extend(converted)
            continue
        node = {"tag": tag, "children": converted}
        if tag == "a":
            href = child.get("href", "")
            if href.startswith("https://") or href.startswith("http://"):
                node["attrs"] = {"href": href}
            else:
                nodes.extend(converted)
                continue
        nodes.append(node)
    return nodes


def create_telegraph_article(article):
    request = urllib.request.Request(
        article["url"],
        headers={"User-Agent": "Mozilla/5.0 (compatible; HabrTelegramDigest/1.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            page_html = response.read().decode("utf-8", errors="replace")
    except Exception as error:
        raise RuntimeError(f"Не удалось загрузить полную статью Habr: {error}") from error

    soup = BeautifulSoup(page_html, "html.parser")
    body = (
        soup.select_one(".tm-article-body")
        or soup.select_one("article .article-formatted-body")
        or soup.select_one("article")
        or soup.select_one("main")
    )
    if body is None:
        raise RuntimeError("Не найден основной текст статьи Habr; Telegraph-страница не создана")
    for unwanted in body.select("script, style, .tm-article-snippet, .tm-user-card, .article__footer, .tm-article-block__share"):
        unwanted.decompose()

    content = telegraph_nodes(body)
    if not content:
        raise RuntimeError("В основной части статьи Habr нет переносимого содержимого")

    token = os.getenv("TELEGRAPH_ACCESS_TOKEN")
    if not token:
        token = telegraph_api("createAccount", {
            "short_name": "HabrDigest",
            "author_name": "Habr Digest",
        })["access_token"]

    title = article["title"][:256]
    author_url = article["url"]
    author_name = (article.get("author") or "Автор на Habr")[:128]
    result = telegraph_api("createPage", {
        "access_token": token,
        "title": title,
        "author_name": author_name,
        "author_url": author_url,
        "content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
        "return_content": "false",
    })
    return result["url"]


def send_message(article):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    excerpt = article["description"]
    if len(excerpt) > 700:
        excerpt = excerpt[:697].rsplit(" ", 1)[0] + "..."

    title = html.escape(article["title"])
    excerpt = html.escape(excerpt)
    tags = " · ".join(html.escape(label) for label in article["labels"])
    author = html.escape(article.get("author") or "не указан в RSS")

    # Publish the article body to Telegraph, then link to the readable full-text page.
    full_text_url = create_telegraph_article(article)
    safe_url = html.escape(full_text_url, quote=True)
    source_url = html.escape(article["url"], quote=True)
    message = (
        f"<b>{title}</b>\n\n"
        f"{excerpt}\n\n"
        f"<b>Автор:</b> {author}\n"
        f"{tags}\n\n"
        f"<a href=\"{safe_url}\">Открыть полный текст в Telegraph</a>\n"
        f"<a href=\"{source_url}\">Оригинал на Habr</a>"
    )
    telegram_api("sendMessage", token, {
        "chat_id": CHANNEL,
        "text": message,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": False},
    })


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    verify_channel(token)
    articles = fetch_articles()
    old_state = {}
    if STATE_FILE.exists():
        old_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    seen = set(old_state.get("seen_urls", []))
    is_first_run = not seen
    new_articles = [article for article in articles if article["url"] not in seen]
    relevant = [article for article in new_articles if article["labels"]]

    if is_first_run:
        # On first run, do not flood the channel with old articles.
        relevant = relevant[-MAX_INITIAL_POSTS:]

    sent_urls = set(old_state.get("posted_urls", []))
    # Publish at most one article per hourly run; newest eligible article first.
    relevant = relevant[-MAX_POSTS_PER_RUN:]
    for article in relevant:
        if article["url"] in sent_urls:
            continue
        print(f"Публикация: {article['title']} [{', '.join(article['labels'])}]")
        send_message(article)
        sent_urls.add(article["url"])

    seen.update(article["url"] for article in articles)
    state = {
        "seen_urls": list(seen)[-MAX_SEEN_URLS:],
        "posted_urls": list(sent_urls)[-MAX_SEEN_URLS:],
    }
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"RSS-статей: {len(articles)}; новых: {len(new_articles)}; "
        f"подходящих к публикации: {len(relevant)}; канал: {CHANNEL}"
    )


if __name__ == "__main__":
    main()
