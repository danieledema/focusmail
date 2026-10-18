# FocusMail

A little focus. A lighter inbox.

A self-hosted, single-user email client that puts one conversation in front of you at a time. Built with React 18, Vite, Tailwind, FastAPI, and SQLite. Email connections use IMAP and SMTP with Google/Microsoft OAuth or manual password authentication; the optional thinking partner uses the OpenAI API.

## Desktop app (Tauri 2)

The desktop app reuses the React interface and bundles the Python backend as a private helper process. No Docker, separately installed Python, `.env`, or app login is needed by an installed build. Add mail accounts under Settings using Google OAuth, Microsoft OAuth, or manual IMAP/SMTP, and add an optional OpenAI key under Preferences. **Sync inbox is the only way to fetch new mail.** A requested sync saves inbox messages progressively before importing only the last 24 hours of Sent history. Older locally cached Sent copies are removed on manual sync; provider mail is untouched. The interface stays available and shows folder progress; a later Sent error does not discard the inbox already downloaded. Closing the window or quitting the app stops its backend; it installs no startup service or tray agent.

Credential storage is platform-independent: email passwords, OAuth refresh/access tokens, Google OAuth client secrets, and your saved OpenAI key are Fernet-encrypted in the local SQLite database. A generated `credential.key` stays alongside it, and both persist across app launches and computer reboots. No macOS Keychain, Windows Credential Manager, or Linux Secret Service is required. This protects secrets in a database-only copy; someone who can read the entire profile, including its key, can decrypt them. The desktop profile uses owner-only permissions on Unix and the user's application-data directory on Windows. Back up both the database and key. The database contains readable email content.

Desktop profiles (stable identifier `com.focusmail.desktop`):

| Platform | Profile location |
| --- | --- |
| macOS | `~/Library/Application Support/com.focusmail.desktop/` |
| Windows | `%APPDATA%\com.focusmail.desktop\` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/com.focusmail.desktop/` |

Desktop profiles are separate from Docker volumes and the development `data/` folder; existing mail is not silently copied. To migrate an existing real profile, quit both versions, then copy its `focusmail.db` and matching `credential.key` into the desktop profile. If you used an external `CREDENTIAL_KEY`, save that exact key as `credential.key` instead. Do not migrate a sample inbox into a real profile. When the app is shut down cleanly, SQLite checkpoints its WAL; when backing up a running app, use SQLite's backup API or include its WAL/SHM consistently.

### Build or run from source

Install Node.js 22+, pnpm 11.25.0, Python 3.12+, and Rust stable. Native compiler prerequisites differ by OS: Xcode Command Line Tools on macOS, Microsoft C++ Build Tools and WebView2 on Windows, and WebKitGTK 4.1 plus development libraries on Linux. See [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/).

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-desktop.txt
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend desktop:dev
```

On Windows, use `python` and `.venv\Scripts\python.exe -m pip` instead of the Unix Python commands. The preparation script discovers the project virtual environment automatically; `FOCUSMAIL_PYTHON` can select another Python executable. Tauri's commands build the UI and freeze the Python runtime before compiling Rust. Desktop development uses that bundled UI rather than Vite hot reload; rerun after edits.

```sh
pnpm --dir frontend desktop:build
```

Installers appear in `frontend/src-tauri/target/release/bundle/`. On macOS, use `pnpm --dir frontend desktop:build --bundles app` to build just a `.app`. Build on each target operating system and CPU architecture; the bundled Python executable cannot be cross-compiled. The GitHub Actions workflow builds macOS, Windows, and Linux installers when manually dispatched or when a version tag is pushed. Windows and Linux require their own build/test runs; compiling on macOS does not validate those platforms. Distribution signing/notarization requires the publisher's platform-specific credentials and is not configured here.

To explore a sample inbox, set `FOCUSMAIL_DESKTOP_DEMO=1` before starting the desktop app. That uses a separate `demo/` subfolder and makes no mail/AI calls. Normal launches use your real profile. The app opens Settings automatically when that profile has no accounts.

Backend startup failures are recorded in `desktop.log` in the profile directory. The helper binds to a random loopback port, the desktop host exchanges a one-use launch token for an HttpOnly cookie, and other browser sessions cannot read the API without that private session. The app is single-instance and blocks navigation outside its own local backend. Credentials are not exposed through Tauri IPC or passed as process arguments.

## Delegating an email

Add names and email addresses under **Settings → People**, then choose **Delegate** in the focus inbox. Select a saved person, write a message, and review the latest received email before forwarding. **Review forward** opens a dialog showing the sending account and recipient, with **Send & archive**, **Send only**, and **Cancel**. Cancellation preserves the draft. Forwarding uses the account that received the original, sends your note above the original text and links, and does not copy the original sender or other recipients. Attachments are not currently stored or forwarded. People are saved locally and persist across app restarts.

**Send & archive** moves the original to that account’s archive and records it in the **Delegated** collection. **Send only** leaves it in the focus inbox. A new incoming message returns a delegated conversation to the inbox. A failed SMTP send leaves the conversation untouched; if sending succeeds but archiving fails, FocusMail reports that the forward was sent and must not be resent. Demo delegation saves a local sample forward without contacting a mail server.

## OpenAI-compatible AI servers

Under **Settings → Preferences**, the **AI API base URL** defaults to `https://api.openai.com/v1`. Replace it with your own base URL, such as `https://your-server.example/v1`, choose a model offered by that server, and enter its secret in **AI server API key**. Save once to apply the URL, model, and key together. The app appends `/chat/completions`; it also accepts a pasted full Chat completions endpoint and normalizes it to the base. Keyless custom servers may leave the key blank.

Secrets are encrypted and scoped to the normalized API base URL. Changing servers never reuses the previous server’s key; switching back restores that server’s saved key. Existing OpenAI keys remain available for the default OpenAI endpoint. `OPENAI_BASE_URL` and `OPENAI_API_KEY` also support environment-based configuration; the environment key is only used with its matching environment base URL.

The selected endpoint receives the selected thread and discussion when you ask the thinking partner. Requests use the OpenAI Chat completions request shape, including JSON object response format and `max_completion_tokens`. Custom servers returning ordinary text can still answer, but structured draft extraction requires the JSON response. TLS certificates are verified, redirects are not followed, and failures never fall back to OpenAI or another server.

AI replies stream as they arrive. **AI reply timeout** in Preferences defaults to **300 seconds** and can be set from 30 to 1800 seconds. This is a total deadline, including thinking and streaming, so keepalive bytes cannot leave a request running forever. Establishing a connection has a separate 15-second timeout. The chat shows elapsed time and whether it is connecting, has sent the request, or is receiving answer text. A sent request does not prove the model is actively thinking: it may be queued or stalled. Connection/TLS failures, interrupted connections, provider errors, and reply timeouts have different messages. **Cancel request** closes the upstream connection and discards incomplete text; the provider may still continue its own processing. Navigating to another thread also cancels an unfinished request. Partial drafts cannot be used until the complete reply has been validated. Compatible servers that return a buffered JSON response instead of streaming are also supported.

## Google and Microsoft sign-in setup

OAuth is implemented for the Tauri desktop app and for a browser running on the **same computer as the Python backend**. The public-client flow uses PKCE, a per-attempt state and nonce, verified signed ID tokens, and a temporary loopback callback listener. The desktop app opens the system browser, never an embedded provider login. Completing sign-in saves the account without fetching email; **Sync inbox** remains a separate action. Access tokens refresh only when a requested mail operation needs one. Cancel, denial, expiry, missing permissions, and revoked sign-in are handled in the account UI.

Both providers require a registered application identifying FocusMail. Those IDs cannot be invented by the code: configure them once under **Settings → Email accounts → Google/Microsoft App setup**. Client IDs are public identifiers; Google’s optional client secret and all tokens are saved encrypted. Never enter your email password in an OAuth setup field. For a distributed product, the publisher normally supplies these app registrations; for your personal/self-hosted build, you can register them yourself.

### Google

1. In your Google Cloud project, configure Google Auth Platform branding, audience, and data access. Request `openid`, `email`, and `https://mail.google.com/` (the scope required for Gmail IMAP/SMTP).
2. Create an OAuth client with application type **Desktop app**, rather than Web application. This supports the app’s temporary `http://127.0.0.1:<random-port>/oauth/callback` redirect with PKCE.
3. While the OAuth app is in Testing, add the Google accounts you want to connect as test users. Publishing an app using Gmail’s full mail scope can require Google verification and compliance with its user-data policies. Do not assume a personal test registration is ready for public distribution.
4. Paste the generated client ID into FocusMail’s Google App setup. If Google supplies a desktop client secret, paste that too; it is not your email password. Save, then select **Connect Google** and review the provider’s consent screen yourself.

Google desktop registrations are public clients: the optional desktop client secret is not treated as a security boundary. PKCE and verified identity protect the code exchange. [Google desktop OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app), [Gmail OAuth/IMAP scopes](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol).

### Microsoft

1. In Microsoft Entra **App registrations**, register FocusMail with an audience supporting **Accounts in any organizational directory and personal Microsoft accounts** if you want both work/school and Outlook.com/Hotmail accounts.
2. Under Authentication, add a **Mobile and desktop applications** platform and this redirect URI: `http://localhost/oauth/callback`. Microsoft ignores the ephemeral port when matching localhost native-app redirects. Do not register this as a Web or SPA application. This implementation uses an authorization-code public client with PKCE and no Microsoft client secret.
3. Add **delegated** Office 365 Exchange Online permissions `IMAP.AccessAsUser.All` and `SMTP.Send`. FocusMail also requests `openid`, `profile`, `email`, and `offline_access` to verify identity and retain sign-in. Do not substitute Microsoft Graph mail permissions: these tokens are used with Exchange IMAP/SMTP.
4. Copy the Application (client) ID into Microsoft App setup, choose personal/work account support, save, then select **Connect Microsoft**. Review sign-in and consent in your browser.

An organization may require administrator consent and may disable IMAP or SMTP AUTH even after OAuth succeeds. Do not weaken organization security settings just to complete setup; consult the administrator or use an allowed account. [Microsoft OAuth mail guide](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth), [localhost redirect matching](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url#localhost-exceptions).

### Reconnecting, storage, and deployment limits

Use **Reconnect** on an OAuth account if the provider revokes access. Reconnecting must select the same verified provider identity; connecting another account is a separate action. Existing manual entries for the same verified address and exact provider IMAP host can be upgraded in place. Other manual accounts, cached mail, and account-specific Archive/Trash handling are preserved. Saved tokens survive app restarts and reboots with the database and encryption key. Changing your registered client ID can require reconnecting accounts.

The temporary callback listens only on the backend computer’s loopback interface, for up to ten minutes. A remote browser or an ordinary Docker port mapping cannot reach that additional listener. **Remote/container OAuth redirects are not implemented**; use the native app/direct local Python runtime for Google/Microsoft OAuth, or retain manual IMAP in Docker. A future remote deployment needs separate Web OAuth registrations and a fixed HTTPS callback. Merely exposing more ports is not the recommended solution.

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
3. Close Settings and click **Sync inbox** when you want to fetch mail. There is no automatic sync at startup or in the background. Initial imports of large mailboxes can take longer; later syncs skip known messages.
4. Read the oldest conversation and choose **Reply**, **Snooze**, **Archive**, or **Delete**.

Use your provider’s documented mail settings. Providers may require an app password and IMAP access to be enabled. Google and Microsoft OAuth are available for local desktop/browser use after the one-time app setup below. Manual IMAP/SMTP remains available.

## Try the sample inbox

Set `DEMO_MODE=true` in `.env`, then start Docker as above. Eight fictional conversations let you explore replies, snoozes, undo, AI-style prompts, and achievements. Demo responses are canned, replies are only saved locally, and no mail server or AI provider is contacted. Real account connections are disabled. Demo mode bypasses authentication, so keep it on localhost.

Use a separate data directory or Docker volume for a demo instance. Switching between demo and real mode does not erase existing data; do not reuse a sample database for real email. **Reset demo inbox** in Settings (or **Try the demo again** after clearing the queue) resets sample mail, points, and badges.

## What’s included

- One oldest-first queue across accounts; returning snoozes come first.
- Chronological, plain-text conversations. Earlier messages collapse individually. HTML email is converted to text; remote images, scripts, and tracking pixels are never rendered. Attachments are not downloaded or displayed.
- Clickable paper titles and web/email links, with safe text rendering rather than active email HTML. Older cached inbox bodies regain preserved HTML links on the next manual sync.
- Inbox and the last 24 hours of special-use Sent-folder history, grouped by References/In-Reply-To with a normalized subject fallback. Moves in other email clients are reconciled on sync. A Sent folder must advertise the IMAP `\\Sent` special-use flag to be imported.
- Editable replies with threading headers. Sending a reply first asks whether to archive the original conversation: **Send & archive** archives it in the receiving account (Gmail uses its localized All Mail folder); **Send only** keeps it in the inbox. A successful SMTP send is saved before archive is attempted, so an archive failure never invites a duplicate send. FocusMail keeps its own sent copy; it does not append a second copy to the provider’s Sent folder. Some SMTP providers save outgoing mail automatically; others do not.
- Six snooze presets in your device’s timezone, stored in SQLite. Local maintenance checks every 10 seconds; the browser refreshes cached state every 15 seconds. Neither fetches mail. After 5 pm, “Later today” uses 5 pm the following day.
- Archive logs into the conversation’s receiving account and moves messages to that same account’s special-use Archive folder or a common provider equivalent. Accounts are never mixed. Delete waits 15 seconds before moving to Trash, allowing a local undo. Pending deletes survive restarts. After that window, use your provider’s Trash to recover mail. Failed deletes restore the thread and reverse its points.
- Safe IMAP moves using MOVE or UIDPLUS. The fallback expunges only this thread’s UIDs; it never runs a mailbox-wide EXPUNGE. Servers without either capability cannot perform archive/delete.
- Session-scoped companion chat, six prompt templates, and an explicit **Use this draft** handoff. Only the current thread’s most recent eight messages, capped at 45,000 characters, plus recent chat history are shared with OpenAI when you ask. Drafts always require your review and send action. Email works without an API key.
- Session points (+3 reply, +2 archive, +1 delete, −1 snooze), persistent daily goal, calendar-day streak, seven achievements, and an Inbox Zero celebration. A page reload starts a new score session and clears chat. Streak days use `TZ` (default Europe/London). Reaching an empty queue after processing something fills the daily goal even if you started with fewer than the target.
- Settings for accounts, goal, AI model, and achievements. Mail fetching happens only through **Sync inbox**. Also includes keyboard shortcuts, a quiet breathing break, mobile layouts, focus indicators, and reduced-motion support.

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

One Uvicorn worker is intentional: explicit sync and mail mutations share an in-process lock. Do not scale this single-user instance to multiple workers or replicas. Schema initialization and the additive v1 sent-folder migration run automatically. Provider-specific folder names/IMAP extensions can vary; connection errors appear in Settings. Removing an account deletes its local cached mail and credentials, not mail on the provider.

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

Keyboard shortcuts: `r` reply, `s` snooze, `a` archive, `d` delete, `?` help, `esc` close a dialog. Shortcuts are inactive while typing or viewing a dialog.

## Connection troubleshooting

Adding an account saves it locally; **Test connection** checks secure IMAP and SMTP login without fetching or sending mail. Each result names the failing service. Google’s “Application-specific password required” response means this password-based connection needs a Google app password, created by the account owner with 2-Step Verification enabled. See [Google’s instructions](https://support.google.com/accounts/answer/185833). Some accounts cannot create app passwords; use the Google OAuth option instead.

Use **Update password** on a saved account to replace its encrypted credential, then test again. DNS, timeout, TLS, and authentication errors appear separately. Raw provider responses and credentials are not echoed by the diagnostic endpoint. Successful connection tests do not import messages; use **Sync inbox** separately.

For browser debugging with the same desktop profile, quit the desktop application first:

```sh
.venv/bin/python scripts/browser-debug.py --data-dir "$HOME/Library/Application Support/com.focusmail.desktop" --port 8080
```

Use the one-use launch URL in `data/browser-launch.url` privately in your browser; it redirects to localhost and authenticates that browser session. Do not share the launch URL. The server remains bound to loopback, and no mail is fetched automatically. Ctrl+C stops the server; quit it before reopening the desktop app. On Windows/Linux, pass the corresponding desktop profile path from the table above.
