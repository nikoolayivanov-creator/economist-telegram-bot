import json
import os
import re
import io
import zipfile
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import quote
from PIL import Image

REPO_API = (
    "https://api.github.com/repos/"
    "blleng/The-Economist/git/trees/main?recursive=1"
)
RAW_BASE = "https://raw.githubusercontent.com/blleng/The-Economist/main/"
STATE_FILE = Path("last_issue.json")
ARCHIVE_FILE = Path("archive.json")
CHANNEL = "-1001317033615"


def get_latest_epub():
    request = urllib.request.Request(
        REPO_API, headers={"User-Agent": "Economist-Telegram-Bot"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        tree = json.loads(response.read().decode("utf-8"))

    pattern = re.compile(r"^TE-(\d{4}-\d{2}-\d{2})/TE-\1\.epub$", re.IGNORECASE)
    issues = []
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        match = pattern.match(item["path"])
        if match:
            issues.append((match.group(1), item["path"]))
    if not issues:
        raise RuntimeError("В архиве не найдено ни одного EPUB")
    return max(issues, key=lambda issue: issue[0])


def download_epub(path):
    request = urllib.request.Request(
        RAW_BASE + quote(path, safe="/"),
        headers={"User-Agent": "Economist-Telegram-Bot"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        content = response.read()
    if not content or not content.startswith(b"PK"):
        raise RuntimeError("Скачанный файл не похож на EPUB")
    return content


def extract_cover(epub_bytes):
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
            cover_id = next(
                (node.attrib.get("content") for node in package.iter()
                 if node.tag.endswith("meta") and node.attrib.get("name", "").lower() == "cover"),
                None,
            )
            cover_item = next((node for node in manifest if node.attrib.get("id") == cover_id), None)
            if cover_item is None:
                cover_item = next(
                    (node for node in manifest
                     if "cover-image" in node.attrib.get("properties", "").split()),
                    None,
                )
            if cover_item is None or not cover_item.attrib.get("href"):
                return None
            image_path = "/".join(part for part in (opf_dir, cover_item.attrib["href"]) if part)
            image_path = image_path.replace("\\", "/")
            data = book.read(image_path)
            mime = cover_item.attrib.get("media-type", "image/jpeg")
            return (data, mime) if data and mime.startswith("image/") else None
    except (KeyError, StopIteration, ValueError, zipfile.BadZipFile, ET.ParseError):
        return None


def make_thumbnail(cover):
    if not cover:
        return None
    data, _mime = cover
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((320, 320))
        for quality in (85, 75, 65, 55, 45):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            thumbnail = output.getvalue()
            if len(thumbnail) < 200_000:
                return thumbnail
    raise RuntimeError("Не удалось подготовить миниатюру обложки для Telegram")


def send_document(date, path, content):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    boundary = "----EconomistTelegramBotBoundary"
    filename = f"The_Economist_{date}.epub"
    fields = {
        "chat_id": CHANNEL,
        "caption": f"The Economist — выпуск от {date}",
    }
    body = bytearray()
    for name, value in fields.items():
        body.extend(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode("utf-8")
        )

    cover = extract_cover(content)
    thumbnail = make_thumbnail(cover) if cover else None
    files = []
    if thumbnail:
        files.append(("thumbnail", f"The_Economist_{date}_cover.jpg", thumbnail, "image/jpeg"))
    files.append(("document", filename, content, "application/epub+zip"))

    for field, file_name, file_content, mime_type in files:
        body.extend(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{file_name}\"\r\nContent-Type: {mime_type}\r\n\r\n").encode("utf-8")
        )
        body.extend(file_content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

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
        raise RuntimeError(f"Telegram не принял EPUB: HTTP {error.code}: {details}") from error
    if not result.get("ok"):
        raise RuntimeError(f"Ошибка Telegram: {result}")
    print("Обложка прикреплена миниатюрой к файлу." if thumbnail else "Обложка в EPUB не найдена; файл отправлен без миниатюры.")
    return result


def main():
    date, path = get_latest_epub()
    issue_id = path
    previous_id = None
    if STATE_FILE.exists():
        previous_id = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("path")
    if previous_id == issue_id:
        print(f"Выпуск от {date} уже опубликован. Повтор не нужен.")
        return

    print(f"Найден выпуск: {path}")
    content = download_epub(path)
    print(f"Размер: {len(content) / 1024 / 1024:.1f} МБ")
    send_document(date, path, content)

    archive = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8")) if ARCHIVE_FILE.exists() else []
    if not any(item.get("source_path") == path for item in archive):
        archive.append({"date": date, "filename": f"The_Economist_{date}.epub", "source_path": path})
    archive.sort(key=lambda item: item["date"], reverse=True)
    ARCHIVE_FILE.write_text(json.dumps(archive, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATE_FILE.write_text(json.dumps({"path": issue_id, "date": date}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"EPUB отправлен в {CHANNEL}: {path}")


if __name__ == "__main__":
    main()
