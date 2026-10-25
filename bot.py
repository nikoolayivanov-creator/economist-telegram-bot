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

PRIMARY_REPO_API = "https://api.github.com/repos/blleng/The-Economist/git/trees/main?recursive=1"
PRIMARY_RAW_BASE = "https://raw.githubusercontent.com/blleng/The-Economist/main/"
FALLBACK_REPO_API = "https://api.github.com/repos/hehonghui/awesome-english-ebooks/git/trees/master?recursive=1"
FALLBACK_RAW_BASE = "https://raw.githubusercontent.com/hehonghui/awesome-english-ebooks/master/"
STATE_FILE = Path("last_issue.json")
ARCHIVE_FILE = Path("archive.json")
CHANNEL = "-1001317033615"


def get_latest_epub_from_source(api_url, raw_base, source_name):
    request = urllib.request.Request(
        api_url, headers={"User-Agent": "Economist-Telegram-Bot"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        tree = json.loads(response.read().decode("utf-8"))

    if source_name == "primary":
        pattern = re.compile(r"^TE-(\d{4}-\d{2}-\d{2})/TE-\1\.epub$", re.IGNORECASE)
    else:
        pattern = re.compile(
            r"^01_economist/te_(\d{4})\.(\d{2})\.(\d{2})/TheEconomist\.\1\.\2\.\3\.epub$",
            re.IGNORECASE,
        )

    issues = []
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        match = pattern.match(item["path"])
        if not match:
            continue
        if source_name == "primary":
            date = match.group(1)
        else:
            date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        issues.append((date, item["path"], raw_base, source_name))

    if not issues:
        return None
    return max(issues, key=lambda issue: issue[0])


def get_latest_epub():
    # Search the original source first, then the fallback source.
    candidates = []
    sources = [
        (PRIMARY_REPO_API, PRIMARY_RAW_BASE, "primary"),
        (FALLBACK_REPO_API, FALLBACK_RAW_BASE, "fallback"),
    ]
    for api_url, raw_base, source_name in sources:
        try:
            issue = get_latest_epub_from_source(api_url, raw_base, source_name)
            if issue:
                candidates.append(issue)
                print(f"Последний выпуск в {source_name}: {issue[0]} ({issue[1]})")
            else:
                print(f"В {source_name} не найдено выпусков EPUB.")
        except Exception as error:
            print(f"Не удалось проверить {source_name}: {error}")

    if not candidates:
        raise RuntimeError("Не удалось найти выпуск EPUB ни в одном репозитории")

    # Choose the newest date across both sources. On the same date, the
    # original repository wins because it is added first.
    return max(candidates, key=lambda issue: issue[0])


def download_epub(path, raw_base):
    request = urllib.request.Request(
        raw_base + quote(path, safe="/"),
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
    date, path, raw_base, source_name = get_latest_epub()
    issue_id = path
    archive = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8")) if ARCHIVE_FILE.exists() else []
    previous = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}

    # Dates, not source paths, are the deduplication key: the same issue may
    # appear in both repositories under different filenames.
    already_published = (
        previous.get("date") == date
        or any(item.get("date") == date for item in archive)
    )
    if already_published:
        print(f"Выпуск от {date} уже опубликован. Повтор не нужен.")
        return

    print(f"Найден выпуск: {path} (источник: {source_name})")
    content = download_epub(path, raw_base)
    print(f"Размер: {len(content) / 1024 / 1024:.1f} МБ")
    send_document(date, path, content)

    if not any(item.get("date") == date for item in archive):
        archive.append({
            "date": date,
            "filename": f"The_Economist_{date}.epub",
            "source_path": path,
            "source_repository": "blleng/The-Economist" if source_name == "primary" else "hehonghui/awesome-english-ebooks",
        })
    archive.sort(key=lambda item: item["date"], reverse=True)
    ARCHIVE_FILE.write_text(json.dumps(archive, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATE_FILE.write_text(json.dumps({"path": issue_id, "date": date}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"EPUB отправлен в {CHANNEL}: {path}")


if __name__ == "__main__":
    main()
