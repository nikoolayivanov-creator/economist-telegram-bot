import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FEED_URL = "https://www.economist.com/printedition/rss.xml"
STATE_FILE = Path("last_issue.json")


def get_latest_issue():
    request = urllib.request.Request(
        FEED_URL,
        headers={"User-Agent": "EconomistTelegramNotifier/1.0"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()

    root = ET.fromstring(data)
    channel = root.find("channel")

    if channel is None:
        raise RuntimeError("RSS feed has no channel")

    item = channel.find("item")

    if item is None:
        raise RuntimeError("No issues found in RSS feed")

    def field(name):
        element = item.find(name)
        return element.text.strip() if element is not None and element.text else ""

    title = field("title")
    link = field("link")
    guid = field("guid") or link

    if not title or not link or not guid:
        raise RuntimeError("Issue information is incomplete")

    return {"guid": guid, "title": title, "link": link}


def send_telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = "@basicofinvestments"

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
    }).encode("utf-8")

    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def main():
    issue = get_latest_issue()

    previous_guid = None

    if STATE_FILE.exists():
        previous_guid = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        ).get("guid")

    if issue["guid"] == previous_guid:
        print("No new issue.")
        return

    message = (
        "Новый выпуск The Economist\n\n"
        f"{issue['title']}\n"
        f"{issue['link']}"
    )

    send_telegram(message)

    STATE_FILE.write_text(
        json.dumps({"guid": issue["guid"]}, indent=2),
        encoding="utf-8",
    )

    print(f"Published: {issue['title']}")


if __name__ == "__main__":
    main()