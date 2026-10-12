import html
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
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
    "прибыл", "убыт", "сделка", "слияни", "поглощени", "ipo", "ipo",
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


def send_message(article):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    excerpt = article["description"]
    if len(excerpt) > 500:
        excerpt = excerpt[:497].rsplit(" ", 1)[0] + "..."

    # Escape article content before using Telegram HTML formatting.
    title = html.escape(article["title"])
    excerpt = html.escape(excerpt)
    url = html.escape(article["url"], quote=True)
    tags = " · ".join(html.escape(label) for label in article["labels"])
    message = (
        f"<b>{title}</b>\n\n"
        f"{excerpt}\n\n"
        f"{url}\n\n"
        f"{tags}"
    )
    telegram_api("sendMessage", token, {
        "chat_id": CHANNEL,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
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

    # Mark the current RSS window as processed so unrelated old posts do not
    # become eligible later; keep enough history to suppress duplicate posts.
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
