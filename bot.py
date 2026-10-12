import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path

REPO_API = (
    "https://api.github.com/repos/"
    "blleng/The-Economist/git/trees/main?recursive=1"
)
RAW_BASE = "https://raw.githubusercontent.com/blleng/The-Economist/main/"
STATE_FILE = Path("last_issue.json")
ARCHIVE_FILE = Path("archive.json")
CHANNEL = "@basicofinvestments"


def get_latest_epub():
    request = urllib.request.Request(
        REPO_API,
        headers={"User-Agent": "Economist-Telegram-Bot"},
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        tree = json.loads(response.read().decode("utf-8"))

    pattern = re.compile(
        r"^TE-(\d{4}-\d{2}-\d{2})/TE-\1\.epub$",
        re.IGNORECASE,
    )

    issues = []

    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue

        path = item["path"]
        match = pattern.match(path)

        if match:
            issues.append((match.group(1), path))

    if not issues:
        raise RuntimeError(
            "Не найден ни один EPUB в GitHub-архиве"
        )

    date, path = max(issues, key=lambda issue: issue[0])

    return date, path


def download_epub(path):
    from urllib.parse import quote

    url = RAW_BASE + quote(path, safe="/")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Economist-Telegram-Bot"},
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()

    if not data or not data.startswith(b"PK"):
        raise RuntimeError("Скачанный файл не похож на EPUB/ZIP")

    return data


    send_document(date, path, content)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    boundary = "----EconomistTelegramBotBoundary"

    filename = f"The_Economist_{date}.epub"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{CHANNEL}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n'
        f"The Economist — выпуск от {date}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: application/epub+zip\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{token}/sendDocument"

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Telegram не принял EPUB: HTTP {error.code}: {details}"
        ) from error

    if not result.get("ok"):
        raise RuntimeError(f"Ошибка Telegram: {result}")

    return result


def main():
    date, path = get_latest_epub()
    issue_id = path

    previous_id = None

    if STATE_FILE.exists():
        previous_id = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        ).get("path")

    if previous_id == issue_id:
        print(f"Выпуск от {date} уже опубликован.")
        return

    print(f"Найден новый выпуск: {path}")
    content = download_epub(path)

    print(f"Размер файла: {len(content) / 1024 / 1024:.1f} МБ")
        send_document(date, path, content)

    archive = []
    if ARCHIVE_FILE.exists():
        archive = json.loads(
            ARCHIVE_FILE.read_text(encoding="utf-8")
        )

    if not any(item.get("source_path") == path for item in archive):
        archive.append({
            "date": date,
            "filename": f"The_Economist_{date}.epub",
            "source_path": path,
        })

    archive.sort(key=lambda item: item["date"], reverse=True)

    ARCHIVE_FILE.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    STATE_FILE.write_text(
        json.dumps(
            {"path": issue_id, "date": date},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print(f"EPUB успешно отправлен в {CHANNEL}: {path}")


if __name__ == "__main__":
    main()
