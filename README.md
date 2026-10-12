# economist-telegram-bot

Automations:
- **The Economist**: existing issue monitor.
- **Habr**: existing article monitor.
- **Good Bonds**: twice-daily digest of new messages from the public Telegram group, posted to the private Telegram channel «Заметки».

## Good Bonds setup (one-time)

The Good Bonds reader uses a Telegram user session because a bot cannot be added to the group by the user. The session is a sensitive credential: anyone who gets it may access the Telegram account. Never commit it to this repository or send it in chat.

1. On your Mac, install Python 3.11+ if needed.
2. Open https://my.telegram.org, sign in with your Telegram account, open **API development tools**, and create an app to get an **API ID** and **API hash**.
3. In a local Terminal, clone the repository or download these files, then run:
   ```bash
   python3 -m pip install -r requirements-goodbonds.txt
   python3 generate_telegram_session.py
   ```
4. Enter the API ID and API hash when prompted. Complete Telegram's login-code and two-step-verification prompts. Copy the resulting session string directly into a GitHub Actions secret. Do not paste it into messages, source code, or files in the repository.
5. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `TELEGRAM_API_ID` — the API ID from my.telegram.org
   - `TELEGRAM_API_HASH` — the API hash from my.telegram.org
   - `TELEGRAM_SESSION_STRING` — the generated session string
   - `OPENAI_API_KEY` — an OpenAI API key (API billing is separate from ChatGPT subscriptions)
   - `TELEGRAM_BOT_TOKEN` — already used by the existing bot; keep the current secret
6. Confirm that the Telegram account used to create the session is a member of the public Good Bonds group and can read its messages.
7. Open **Actions → Good Bonds Digest → Run workflow** to test the first run.

The workflow runs twice daily at 06:00 and 16:00 UTC (approximately 08:00 and 18:00 in Stockholm during summer, one hour earlier in winter). Change the cron schedule in `.github/workflows/goodbonds-digest.yml` if you prefer different times.

The digest highlights discussion points, issuers and bond issues, rates/coupons/offers when mentioned, risks, and disagreements. It is a summary of user messages, not verified financial research or individual investment advice. No post is sent when no new text messages are found.

If the Telegram session is exposed, terminate it immediately in Telegram **Settings → Devices**.
