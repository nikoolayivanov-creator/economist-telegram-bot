import json
import os
import re
import urllib.request
import urllib.error
import zipfile
import io
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

REPO_API = "https://api.github.com/repos/hehonghui/awesome-english-ebooks/git/trees/master?recursive=1"
RAW_BASE = "https://raw.githubusercontent.com/hehonghui/awesome-english-ebooks/master/"
STATE_FILE = Path("wired_last_issue.json")
ARCHIVE_FILE = Path("wired_archive.json")
CHANNEL = "@basicofinvestments"


def get_latest_epub():
    request = urllib.request.Request(REPO_API, headers={"User-Agent": "Wired-Telegram-Bot"})
    with urllib.request.urlopen(request, timeout=60) as response:
        tree = json.loads(response.read().decode("utf-8"))

    pattern = re.compile(r"^05_wired/(\d{4}\.\d{2}\.\d{2})/wired_\1\.epub$", re.IGNORECASE)
    issues = []
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        match = pattern.match(item["path"])
        if match:
            date = match.group(1).replace(".", "-")
            issues.append((date, item["path"]))

    if not issues:
        raise RuntimeError("В архиве WIRED не найдено ни одного EPUB")
    return max(issues, key=lambda issue: issue[0])


def download_file(path):
    url = RAW_BASE + quote(path, safe="/")
    request = urllib.request.Request(url, headers={"User-Agent": "Wired-Telegram-Bot"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def extract_cover(epub_bytes):
    """Extract the cover image using EPUB metadata; return (bytes, mime) or None."""
    try:
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as book:
            container = ET.fromstring(book.read("META-INF/container.xml"))
            rootfile = next(
                node for node in container.iter()
                if node.tag.endswith("rootfile") and node.attrib.get("full-path")
            )
            opf_path = rootfile.attrib["full-path"]
            opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
            package = ET.fromstring(book.read(opf_path))
            manifest = [node for node in package.iter() if node.tag.endswith("item")]
            cover_id = None

            for node in package.iter():
                if node.tag.endswith("meta") and node.attrib.get("name", "").lower() == "cover":
                    cover_id = node.attrib.get("content")
                    if cover_id:
                        break

            cover_item = next(
                (node for node in manifest if node.attrib.get("id") == cover_id),
                None,
            )
            if cover_item is None:
                cover_item = next(
                    (node for node in manifest
                     if "cover-image" in node.attrib.get("properties", "").split()),
                    None,
                )
            if cover_item is None:
                return None

            href = cover_item.attrib.get("href")
            if not href:
                return None
            image_path = "/".join(part for part in (opf_dir, href) if part)
            image_path = image_path.replace("\\", "/")
            data = book.read(image_path)
            mime = cover_item.attrib.get("media-type", "image/jpeg")
            if not mime.startswith("image/") or not data:
                return None
            return data, mime
    except (KeyError, StopIteration, ValueError, zipfile.BadZipFile, ET.ParseError):
        return None


def telegram_upload(method, fields, file_field=None, filename=None, file_content=None, mime_type=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    boundary = "----WiredTelegramBotBoundary"
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    body = "".join(parts).encode("utf-8")
    if file_field and file_content is not None:
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n"
        ).encode("utf-8")
        body += file_content
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram не принял {method}: HTTP {error.code}: {details}") from error
    if not result.get("ok"):
        raise RuntimeError(f"Ошибка Telegram ({method}): {result}")
    return result


def send_issue(date, path, content):
    # Publish the EPUB first, so an optional cover-photo failure cannot block the issue.
    telegram_upload(
        "sendDocument",
        {"chat_id": CHANNEL, "caption": f"WIRED — выпуск от {date}"},
        file_field="document",
        filename=f"WIRED_{date}.epub",
        file_content=content,
        mime_type="application/epub+zip",
    )
    print(f"EPUB WIRED отправлен в {CHANNEL}")

    cover = extract_cover(content)
    if not cover:
        print("Обложка в EPUB не найдена; выпуск опубликован без неё.")
        return

    cover_data, cover_mime = cover
    ext = "png" if "png" in cover_mime else "jpg"
    try:
        telegram_upload(
            "sendPhoto",
            {"chat_id": CHANNEL, "caption": f"Обложка WIRED — выпуск от {date}"},
            file_field="photo",
            filename=f"WIRED_{date}_cover.{ext}",
            file_content=cover_data,
            mime_type=cover_mime,
        )
        print(f"Обложка WIRED отправлена в {CHANNEL}")
    except RuntimeError as error:
        # The main EPUB has already been delivered; do not fail and resend it next hour.
        print(f"Не удалось отправить обложку, но EPUB опубликован: {error}")


def main():
    date, path = get_latest_epub()
    previous_path = None
    if STATE_FILE.exists():
        previous_path = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("path")

    if previous_path == path:
        print(f"WIRED от {date} уже опубликован. Повтор не нужен.")
        return

    print(f"Найден выпуск WIRED: {path}")
    content = download_file(path)
    if not content.startswith(b"PK"):
        raise RuntimeError("Скачанный файл не похож на EPUB")
    print(f"Размер EPUB: {len(content) / 1024 / 1024:.1f} МБ")

    send_issue(date, path, content)

    archive = []
    if ARCHIVE_FILE.exists():
        archive = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
    if not any(item.get("source_path") == path for item in archive):
        archive.append({
            "date": date,
            "filename": f"WIRED_{date}.epub",
            "source_path": path,
        })
    archive.sort(key=lambda item: item["date"], reverse=True)
    ARCHIVE_FILE.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    STATE_FILE.write_text(
        json.dumps({"path": path, "date": date}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"EPUB WIRED отправлен в {CHANNEL}: {path}")


if __name__ == "__main__":
    main()
