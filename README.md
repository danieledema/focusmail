# FocusMail

A little focus. A lighter inbox.

A self-hosted, single-user email client that puts one conversation in front of you at a time. Built with React 18, Vite, Tailwind, FastAPI, and SQLite. All email connections use IMAP and SMTP; the optional thinking partner uses the OpenAI API.

## Start with Docker

Requires Docker with Compose.

```sh
cp .env.example .env
# Edit .env: set a unique APP_PASSWORD; optionally add OPENAI_API_KEY.
docker compose up --build -d
```

Open [FocusMail on localhost](http://localhost:8080). The browser asks for HTTP Basic Auth: enter any username and the `APP_PASSWORD` from `.env`. A signed, HttpOnly cookie keeps you signed in for seven days. Settings opens on the first run.

1. Add your account name, IMAP host/port, email username, app password, and SMTP host/port.
2. Use **Test connection** to check both IMAP and SMTP. IMAP requires implicit TLS (normally port 993). SMTP uses implicit TLS on port 465 and STARTTLS on other ports (normally 587).
3. Close Settings and click **Check for new mail**. Automatic sync also runs at the configured interval. Initial imports of large mailboxes can take longer; later syncs skip known messages.
4. Read the oldest conversation and choose **Reply**, **Snooze**, **Archive**, or **Delete**.

Use your provider’s documented mail settings. Providers may require an app password and IMAP access to be enabled. OAuth is outside this version’s scope.

## Try the sample inbox

Set `DEMO_MODE=true` in `.env`, then start Docker as above. Eight fictional conversations let you explore replies, snoozes, undo, AI-style prompts, and achievements. Demo responses are canned, replies are only saved locally, and no mail server or AI provider is contacted. Real account connections are disabled. Demo mode bypasses authentication, so keep it on localhost.

Use a separate data directory or Docker volume for a demo instance. Switching between demo and real mode does not erase existing data; do not reuse a sample database for real email. **Reset demo inbox** in Settings (or **Try the demo again** after clearing the queue) resets sample mail, points, and badges.

## What’s included

- One oldest-first queue across accounts; returning snoozes come first.
- Chronological, plain-text conversations. Earlier messages collapse individually. HTML email is converted to text; remote images, scripts, and tracking pixels are never rendered. Attachments are not downloaded or displayed.
- Inbox and special-use Sent-folder history, grouped by References/In-Reply-To with a normalized subject fallback. Moves in other email clients are reconciled on sync. A Sent folder must advertise the IMAP `\\Sent` special-use flag to be imported.
- Editable replies with threading headers. A successful SMTP send is saved before archive is attempted, so an archive failure never invites a duplicate send. FocusMail keeps its own sent copy; it does not append a second copy to the provider’s Sent folder. Some SMTP providers save outgoing mail automatically; others do not.
- Six snooze presets in your device’s timezone, stored in SQLite. The scheduler checks every 10 seconds; the browser also refreshes every 15 seconds. After 5 pm, “Later today” uses 5 pm the following day.
- Archive moves to a special-use Archive folder or a common provider folder. Delete waits 15 seconds before moving to Trash, allowing a local undo. Pending deletes survive restarts. After that window, use your provider’s Trash to recover mail. Failed deletes restore the thread and reverse its points.
- Safe IMAP moves using MOVE or UIDPLUS. The fallback expunges only this thread’s UIDs; it never runs a mailbox-wide EXPUNGE. Servers without either capability cannot perform archive/delete.
- Session-scoped companion chat, six prompt templates, and an explicit **Use this draft** handoff. Only the current thread’s most recent eight messages, capped at 45,000 characters, plus recent chat history are shared with OpenAI when you ask. Drafts always require your review and send action. Email works without an API key.
- Session points (+3 reply, +2 archive, +1 delete, −1 snooze), persistent daily goal, calendar-day streak, seven achievements, and an Inbox Zero celebration. A page reload starts a new score session and clears chat. Streak days use `TZ` (default Europe/London). Reaching an empty queue after processing something fills the daily goal even if you started with fewer than the target.
- Settings for accounts, polling interval, goal, AI model, and achievements. Also includes keyboard shortcuts, a quiet breathing break, mobile layouts, focus indicators, and reduced-motion support.

## Configuration and data

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_PASSWORD` | required in real mode | Shared app password; any Basic Auth username works |
| `OPENAI_API_KEY` | empty | Optional thinking partner credentials |
| `AI_MODEL` | `gpt-4o` | Initial model, overridable in Settings |
| `PORT` | `8080` | Host port used by Compose |
| `TZ` | `Europe/London` | Calendar-day timezone for goals and streaks |
| `DEMO_MODE` | `false` | Explicit sample inbox; disables real connections and authentication |
| `DATA_DIR` | `./data` | SQLite and credential-key directory; `/app/data` in Docker |
| `CREDENTIAL_KEY` | generated | Optional Fernet key to encrypt account passwords |
| `COOKIE_SECURE` | `false` | Set `true` behind an HTTPS reverse proxy |

Compose binds to `127.0.0.1` by default. For access from another device, place it behind an HTTPS reverse proxy and set `COOKIE_SECURE=true`. Preserve the original Host header. The app rejects mutation requests from a different browser origin and does not trust arbitrary forwarded headers. HTTP Basic credentials need HTTPS outside loopback.

The named `focusmail-data` volume contains `focusmail.db` and a generated `credential.key` (unless supplied by environment). Account passwords use authenticated Fernet encryption. **Back up the entire volume and retain an external CREDENTIAL_KEY if configured.** Losing the key means reconnecting the accounts. Mail content itself is stored as plaintext in the local SQLite database.

One Uvicorn worker is intentional: sync and mail mutations share an in-process lock. Do not scale this single-user instance to multiple workers or replicas. Schema initialization and the additive v1 sent-folder migration run automatically. Provider-specific folder names/IMAP extensions can vary; connection errors appear in Settings. Removing an account deletes its local cached mail and credentials, not mail on the provider.

## Local development

Requires Python 3.12+ and Node.js 22+. Install pnpm 11.25.0 (or enable it through Corepack).

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt pytest
cd frontend
pnpm install
pnpm run build
cd ..
```

Run the sample app with the compiled frontend:

```sh
DEMO_MODE=true DATA_DIR=./data/demo .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8080
```

For frontend hot reload, keep that backend running and run `pnpm run dev` in `frontend/`. Vite proxies `/api` to port 8080. For a real account in development, use `uvicorn --env-file .env` with demo mode disabled. Avoid sharing a data directory between concurrently running instances.

The interface uses DM Sans and Manrope from Google Fonts with system fallbacks. Email and app behavior still work when those fonts are unavailable.

## Verification

```sh
.venv/bin/python -m pytest backend/tests -q
cd frontend
pnpm run build
```

Tests use an isolated temporary database and fictional mail. They cover actions and duplicate protection, failed SMTP/archive handling, undo and delayed delete, snooze scheduling and DST, credential encryption, Basic Auth/cookies, origin checks, IMAP folder moves/UIDPLUS, thread grouping and sent history, settings, goal/streak/achievement thresholds, and demo AI drafts. Live provider credentials and an OpenAI key are required for real integration checks.

Keyboard shortcuts: `R` reply, `S` snooze, `E` archive, `Delete` delete, `?` help, `Esc` close a dialog. Shortcuts are inactive while typing or viewing a dialog.
