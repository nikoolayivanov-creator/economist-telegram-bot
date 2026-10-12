"""Run locally to authorize your own Telegram account and create a StringSession.

Never commit or share the printed session string. Anyone with it can access
your Telegram account. Save it only as the private GitHub Actions secret
TELEGRAM_SESSION_STRING.
"""
import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def main():
    api_id = os.getenv("TELEGRAM_API_ID") or input("Telegram API ID: ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("Telegram API hash: ").strip()
    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        print("\nУспешная авторизация Telegram.")
        print("Добавь следующую строку в GitHub Actions secret TELEGRAM_SESSION_STRING:")
        print(client.session.save())
        print("\nНе отправляй эту строку в чат и не сохраняй её в репозитории.")


if __name__ == "__main__":
    main()
