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
from PIL import Image

REPO_API = "https://api.github.com/repos/hehonghui/awesome-english-ebooks/git/trees/master?recursive=1"
RAW_BASE = "https://raw.githubusercontent.com/hehonghui/awesome-english-ebooks/master/"
STATE_FILE = Path("wired_last_issue.json")
ARCHIVE_FILE = Path("wired_archive.json")
CHANNEL = "-1001317033615"


def get_latest_epub():
    request = urllib.request.Request(REPO_API, headers={"User-Agent": "Wired-Telegram-Bot"})
    with urllib.request.urlopen(request, timeout=60) as response:
        tree = json.loads(response.read().decode("utf-8"))
    pattern = re.compile(r"^05_wired/(\d{4}\.\d{2}\.\d{2})/wired_\1\.epub$", re.IGNORECASE)
    issues = []
    for item in tree.get("tree", []):
        if item.get("type") == "blob":
            match = pattern.match(item["path"])
            if match:
                issues.append((match.group(1).replace(".", "-"), item["path"]))
    if not issues:
        raise RuntimeError("В архиве WIRED не найдено ни одного EPUB")
    return max(issues, key=lambda issue: issue[0])


def download_file(path):
    request = urllib.request.Request(RAW_BASE + quote(path, safe="/"), headers={"User-Agent": "Wired-Telegram-Bot"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def extract_cover(epub_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as book:
            container = ET.fromstring(book.read("META-INF/container.xml"))
            rootfile = next(n for n in container.iter() if n.tag.endswith("rootfile") and n.attrib.get("full-path"))
            opf_path = rootfile.attrib["full-path"]
            opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
            package = ET.fromstring(book.read(opf_path))
            manifest = [n for n in package.iter() if n.tag.endswith("item")]
            cover_id = next((n.attrib.get("content") for n in package.iter()
                             if n.tag.endswith("meta") and n.attrib.get("name", "").lower() == "cover"), None)
            cover_item = next((n for n in manifest if n.attrib.get("id") == cover_id), None)
            if cover_item is None:
                cover_item = next((n for n in manifest if "cover-image" in n.attrib.get("properties", "").split()), None)
            if cover_item is None or not cover_item.attrib.get("href"):
                return None
            image_path = "/".join(part for part in (opf_dir, cover_item.attrib["href"]) if part).replace("\\", "/")
            data = book.read(image_path)
            mime = cover_item.attrib.get("media-type", "image/jpeg")
            return (data, mime) if mime.startswith("image/") and data else None
    except (KeyError, StopIteration, ValueError, zipfile.BadZipFile, ET.ParseError):
        return None


def make_thumbnail(cover):
    """Telegram document thumbnails must be JPEG, under 200 KB, max 320x320."""
    if not cover:
        return None
    image_bytes, _mime = cover
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        image.thumbnail((320, 320))
        for quality in (85, 75, 65, 55, 45):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            data = output.getvalue()
            if len(data) < 200_000:
                return data
    raise RuntimeError("Не удалось подготовить миниатюру обложки WIRED для Telegram")


def telegram_upload(method, fields, files):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    boundary = "----WiredTelegramBotBoundary"
    body = bytearray()
    for name, value in fields.items():
        body.extend((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode("utf-8"))
    for field, filename, content, mime_type in files:
        body.extend((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\nContent-Type: {mime_type}\r\n\r\n").encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
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
    cover = extract_cover(content)
    thumbnail = make_thumbnail(cover) if cover else None
    fields = {"chat_id": CHANNEL, "caption": f"WIRED — полный выпуск от {date}"}
    files = [("document", f"WIRED_{date}.epub", content, "application/epub+zip")]
    if thumbnail:
        files.append(("thumbnail", f"WIRED_{date}_cover.jpg", thumbnail, "image/jpeg"))
        print(f"Обложка подготовлена как миниатюра документа ({len(thumbnail) / 1024:.0f} КБ).")
    else:
        print("Обложка в EPUB не найдена; отправляю файл без миниатюры.")
    telegram_upload("sendDocument", fields, files)
    print(f"EPUB WIRED с миниатюрой отправлен в канал {CHANNEL}" if thumbnail else f"EPUB WIRED отправлен в канал {CHANNEL}")


def main():
    date, path = get_latest_epub()
    previous_path = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("path") if STATE_FILE.exists() else None
    if previous_path == path:
        print(f"WIRED от {date} уже опубликован. Повтор не нужен.")
        return
    print(f"Найден выпуск WIRED: {path}")
    content = download_file(path)
    if not content.startswith(b"PK"):
        raise RuntimeError("Скачанный файл не похож на EPUB")
    print(f"Размер EPUB: {len(content) / 1024 / 1024:.1f} МБ")
    send_issue(date, path, content)
    archive = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8")) if ARCHIVE_FILE.exists() else []
    if not any(item.get("source_path") == path for item in archive):
        archive.append({"date": date, "filename": f"WIRED_{date}.epub", "source_path": path})
    archive.sort(key=lambda item: item["date"], reverse=True)
    ARCHIVE_FILE.write_text(json.dumps(archive, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATE_FILE.write_text(json.dumps({"path": path, "date": date}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Выпуск WIRED от {date} опубликован в канале {CHANNEL}: {path}")


if __name__ == "__main__":
    main()
