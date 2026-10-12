import json
import os
import re
import subprocess
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

OUTPUT = Path("barrons-latest.epub")
STATE_FILE = Path("last_barrons_issue.json")
CHANNEL = "@basicofinvestments"


def build_epub():
    recipe = Path("barrons.recipe")
    request = urllib.request.Request(
        "https://raw.githubusercontent.com/kovidgoyal/calibre/master/recipes/barrons.recipe",
        headers={"User-Agent": "Barrons-Telegram-Bot"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        recipe.write_bytes(response.read())

    subprocess.run(
        ["ebook-convert", str(recipe), str(OUTPUT)],
        check=True,
        timeout=900,
    )

    if not OUTPUT.is_file() or OUTPUT.stat().st_size < 1000:
        raise RuntimeError("EPUB не создан или файл слишком мал")

    with zipfile.ZipFile(OUTPUT) as epub:
        bad_member = epub.testzip()
        if bad_member:
            raise RuntimeError(f"Повреждён EPUB: {bad_member}")

        opf_name = next(
            (name for name in epub.namelist() if name.endswith(".opf")),
            None,
        )
        if not opf_name:
            raise RuntimeError("В EPUB не найден content.opf")

        root = ET.fromstring(epub.read(opf_name))
        title = next(
            (
                node.text or ""
                for node in root.iter()
                if node.tag.rsplit("}", 1)[-1] == "title"
            ),
            "",
        )

    match = re.search(r"\[(.+?)\]", title)
    if not match:
        raise RuntimeError(f"Не удалось определить дату выпуска: {title!r}")

    issue_date = match.group(1).strip()
    print(f"Создан выпуск: {title}")
    print(f"Размер EPUB: {OUTPUT.stat().st_size / 1024:.1f} КБ")
    return issue_date


def send_document(issue_date):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    boundary = "----BarronsTelegramBotBoundary"
    filename = f"Barrons_{issue_date.replace(' ', '_').replace(',', '')}.epub"
    caption = (
        f"Barron's — выпуск от {issue_date}\n"
        "Электронная версия: доступные заголовки и фрагменты статей."
    )

    fields = [
        ("chat_id", CHANNEL),
        ("caption", caption),
    ]
    body = bytearray()
    for name, value in fields:
        body.extend(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )

    body.extend(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
        "Content-Type: application/epub+zip\r\n\r\n".encode("utf-8")
    )
    body.extend(OUTPUT.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Telegram не принял Barron's EPUB: HTTP {error.code}: {details}"
        ) from error

    if not result.get("ok"):
        raise RuntimeError(f"Telegram вернул ошибку: {result}")

    print(f"Barron's EPUB отправлен в {CHANNEL}")


def main():
    issue_date = build_epub()

    previous_date = None
    if STATE_FILE.exists():
        previous_date = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        ).get("date")

    if previous_date == issue_date:
        print(f"Выпуск {issue_date} уже опубликован; повтор пропущен.")
        return

    send_document(issue_date)

    STATE_FILE.write_text(
        json.dumps({"date": issue_date}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
