import html
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

CHAT = os.getenv("GOODBONDS_CHAT", "GoodBonds")
DESTINATION = os.getenv("TELEGRAM_DESTINATION", "-1001317033615")
STATE_FILE = Path("goodbonds_state.json")
MAX_MESSAGES = 500
MAX_INPUT_CHARS = 90000


def read_state():
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def telegram_send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = {
        "chat_id": DESTINATION,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram sendMessage HTTP {error.code}: {detail}") from error
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {result}")
    return result["result"]


def summarize(messages, start_label, end_label):
    api_key = os.environ["OPENAI_API_KEY"]
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    transcript = []
    for msg in messages:
        sender = (msg.sender_name or "Участник").replace("\n", " ").strip()
        body = (msg.text or "").strip()
        if not body:
            continue
        date = msg.date.astimezone(timezone.utc).strftime("%d.%m %H:%M UTC")
        transcript.append(
            f"[{date}; message_id={msg.id}; автор={sender}] {body}"
        )

    source_text = "\n\n".join(transcript)
    source_text = source_text[:MAX_INPUT_CHARS]
    if not source_text.strip():
        return ""

    prompt = (
        "Ты аналитик обсуждений российского рынка облигаций. Сделай точное саммари "
        "сообщений группового чата Good Bonds за указанный период. Пиши по-русски, "
        "кратко и предметно. Не придумывай факты, цены, доходности или выводы. "
        "Отделяй подтверждённые факты от мнений участников и слухов. "
        "Отметь конкретные эмитенты/выпуски, купоны, доходности, оферты, рейтинги, "
        "долги, ликвидность и риски, только если они действительно упомянуты. "
        "Включи разделы: «Главное», «Эмитенты и выпуски», «Риски и спорные мнения», "
        "«Что проверить». Если разделу нечего добавить, пропусти его. "
        "Не давай персональных инвестиционных рекомендаций. В конце напомни, "
        "что это пересказ обсуждения, а не подтверждённая аналитика. "
        "Не цитируй большие фрагменты сообщений дословно. "
        f"Период: {start_label} — {end_label}.\n\n"
        f"Сообщения чата:\n{source_text}"
    )
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": 1400,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API HTTP {error.code}: {detail}") from error

    pieces = []
    for item in result.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                pieces.append(content["text"])
    return "\n".join(pieces).strip()


async def main_async():
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_string = os.environ["TELEGRAM_SESSION_STRING"]
    state = read_state()
    last_id = int(state.get("last_message_id", 0))
    now = datetime.now(timezone.utc)

    async with TelegramClient(StringSession(session_string), api_id, api_hash) as client:
        entity = await client.get_entity(CHAT)
        messages = []
        async for msg in client.iter_messages(entity, limit=MAX_MESSAGES):
            if not msg or not msg.id or not msg.date:
                continue
            if last_id:
                if msg.id > last_id:
                    messages.append(msg)
            elif msg.date >= now - timedelta(hours=12):
                messages.append(msg)

        messages.sort(key=lambda msg: msg.id)
        messages = [m for m in messages if (m.message or "").strip()]
        if not messages:
            print("За период новых текстовых сообщений нет; публикация пропущена.")
            return

        start = messages[0].date.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        end = messages[-1].date.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        digest = summarize(messages, start, end)
        if not digest:
            print("В новых сообщениях нет текста для саммари; публикация пропущена.")
            return

        first_line = f"<b>Good Bonds — саммари</b>\n<i>{html.escape(start)} — {html.escape(end)}</i>\n\n"
        footer = (
            "\n\n<i>Пересказ обсуждения, а не подтверждённая аналитика "
            "и не индивидуальная инвестиционная рекомендация.</i>"
        )
        message = first_line + html.escape(digest) + footer
        # Telegram's sendMessage limit is 4096 characters; keep a safety margin.
        if len(message) > 3900:
            message = message[:3880].rsplit(" ", 1)[0] + "…"
        telegram_send(message)

        state = {
            "last_message_id": max(m.id for m in messages),
            "last_digest_at": now.isoformat(),
        }
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"Опубликовано саммари: {len(messages)} сообщений; "
            f"последний ID={state['last_message_id']}"
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main_async())
