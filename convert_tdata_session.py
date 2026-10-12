#!/usr/bin/env python3
"""Convert an already-authorized Telegram Desktop tdata folder into Telethon credentials.

Run locally only. Never commit or send the generated session string; it grants access
to the Telegram account.
"""
import argparse
import getpass
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Read an existing Telegram Desktop tdata folder and print local session credentials."
    )
    parser.add_argument(
        "tdata_path",
        help="Path to Telegram Desktop's tdata folder (from an already logged-in desktop app)",
    )
    parser.add_argument(
        "--passcode",
        action="store_true",
        help="Prompt for the local Telegram Desktop passcode if tdata is protected",
    )
    args = parser.parse_args()

    try:
        from tdata_reader import read_tdata
    except ImportError:
        print("Missing dependency. Run: python3 -m pip install -r requirements-tdata.txt", file=sys.stderr)
        raise SystemExit(2)

    path = Path(args.tdata_path).expanduser()
    if not path.exists() or not path.is_dir():
        print(f"tdata folder not found: {path}", file=sys.stderr)
        raise SystemExit(2)

    passcode = getpass.getpass("Telegram Desktop local passcode: ") if args.passcode else None
    try:
        account = read_tdata(str(path), passcode=passcode)
        session_string = account.to_string_session()
    except Exception as exc:
        print(f"Could not read tdata: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print("\nCredentials extracted locally. Do NOT paste them into chat or commit them.")
    print("\nTELEGRAM_API_ID=" + str(account.api_id))
    print("TELEGRAM_API_HASH=" + str(account.api_hash))
    print("TELEGRAM_SESSION_STRING=" + session_string)
    print("\nAdd these as GitHub Actions secrets, then delete this terminal output.")
    print("If this session is exposed, revoke it in Telegram Settings > Devices.")


if __name__ == "__main__":
    main()
