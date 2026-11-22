# Telegram MCP with TDLib

A **read-only**, local MCP server for a personal Telegram account. Python 3.11+,
the official MCP Python SDK, and TDLib's native JSON interface via `ctypes`.
No Node.js, Bot API, Telethon, or intermediary Telegram wrapper.

Python is a good fit here: TDLib handles Telegram networking, encryption, and its
persistent database; Python handles asynchronous request routing and MCP tools.
TDLib is still a native shared-library dependency. The repository includes a
prebuilt library for **macOS 13+**, supporting **Apple Silicon and Intel**. It is
loaded automatically and includes OpenSSL, so no Homebrew libraries are needed
at runtime. Other platforms need a compatible TDLib build.

## Install

For **macOS 13+**, download to `~/.local/trl-telegram-mcp`, install the Python server,
and register it in **[Claude Code](https://code.claude.com/docs/en/mcp#user-scope)**
and **[Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)** with one command:

```sh
curl -fsSL https://raw.githubusercontent.com/whytrall/telegram-mcp/refs/heads/master/install.sh | bash
```

Requires **Python 3.11+**, Git, and curl. The bootstrap clones `master` into
`~/.local/trl-telegram-mcp`, or updates an existing clean checkout on `master`.
It uses the authenticated [GitHub CLI](https://cli.github.com/) when available,
otherwise Git over HTTPS. While this repository is private, the raw script URL
also requires authentication; the command above works anonymously once it is public.
The installer creates `.venv` inside the download and sets the
user-level `telegram` server in `~/.claude.json` and `~/.codex/config.toml`
(`CODEX_HOME` is respected). It replaces an existing `telegram` entry and keeps
other servers and settings. Restart Claude Code and Codex afterward; your first
account read opens the browser login page. No Telegram credentials go in the command.

To update an existing installation, rerun the same command, then stop the shared backend
with `~/.local/trl-telegram-mcp/.venv/bin/telegram-mcp stop` and restart the clients.
For custom config locations,
pass `--claude-config /path/to/claude.json --codex-config /path/to/config.toml` to that script.

For development, clone the repository and install from its root:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

On macOS 13+, that is enough: the server uses the bundled library. To rebuild the
universal macOS binary from pinned sources (Xcode Command Line Tools, Git, Perl,
CMake, and gperf are required):

```sh
brew install cmake gperf
bash scripts/prebuild_tdlib_macos.sh
```

This builds both architectures with a macOS 13 deployment target, links OpenSSL
statically, and writes `src/telegram_mcp/_vendor/macos/libtdjson.dylib`. The adjacent
manifest records the TDLib commit, OpenSSL version, architectures, and SHA-256;
license files are included. Build products and downloaded sources stay in `.deps/`.
`TDLIB_BUILD_JOBS` controls parallelism (default 8 for the prebuild script).

On Debian/Ubuntu, build TDLib locally (C++ compiler, Git, CMake, gperf, OpenSSL,
and zlib are required):

```sh
sudo apt-get install build-essential git cmake gperf libssl-dev zlib1g-dev
bash scripts/build_tdlib.sh
export TDLIB_LIBRARY_PATH="$PWD/.deps/td-build/libtdjson.so"
```

Both scripts build the shared JSON library and pin TDLib to
`03220f6b3043c6fd7785fbf0901d63f19ad19974`. Do not use the old 1.8.0 release:
its authorization parameters differ. An existing compatible library can be used
instead by setting `TDLIB_LIBRARY_PATH`, which overrides the bundled library.
Native builds may take several minutes. For a custom macOS build using Homebrew
OpenSSL, `brew install cmake gperf openssl@3` and `bash scripts/build_tdlib.sh`
remain available (default 4 build jobs).

## Browser login

Start the server and call an account tool such as `list_chats` or `get_me`.
If Telegram is not authorized, the server automatically opens a local browser page:

1. Enter your **App ID** and **App hash** from [Telegram API settings](https://my.telegram.org/apps).
2. Scan the QR image using **Telegram → Settings → Devices → Link Desktop Device**.
3. Enter your two-step verification password or any additional code requested by Telegram.
4. The original tool call continues after login completes.

No credentials are needed to start the MCP server or discover its tools. App credentials,
passwords, and codes are entered on the browser page, never in MCP tool arguments or chat.
The QR image refreshes automatically as Telegram rotates its token. Concurrent tool calls
share one browser login, and cancelling a waiting call does not interrupt the login.
There is no separate MCP login tool.

App credentials are saved in `app-credentials.json` with owner-only permissions inside
`TELEGRAM_DATA_DIR`, alongside TDLib's persistent session. Subsequent launches reuse them.
A complete `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` environment pair overrides saved app
credentials and skips the app-credentials form. An already-authorized session needs no browser.
`telegram_status` is a nonblocking diagnostic; it reports authorization state and an active
login page URL without starting login itself.

Account tool calls wait up to `TELEGRAM_LOGIN_TIMEOUT` seconds (default **300**).
Set your MCP client's tool timeout above this, for example **360 seconds**. If a call
expires, finish login on the existing page and retry it. If automatic browser opening is
unavailable, open the URL returned by `telegram_status` or the timed-out tool.

The login page is served only on `127.0.0.1` with an unpredictable path. Its QR images
are generated locally. It does not use an external QR-generation service.
Registration, Premium purchase, and account recovery must be completed in an official app.

TDLib saves its authorized session in an OS-specific private directory:

| OS | Default location |
| --- | --- |
| macOS | `~/Library/Application Support/telegram-mcp` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/telegram-mcp` |

Set `TELEGRAM_DATA_DIR` to choose another directory. Stdio connections automatically
share one local backend per data directory, so multiple Codex chats can use the same
Telegram session concurrently. The optional `TELEGRAM_DATABASE_KEY` encrypts the TDLib database;
use the same key on every launch. This key does not encrypt the separate app-credentials
file or downloaded/cached files.

Environment variables are read directly; `.env` files are not automatically loaded.
Use `.venv/bin/telegram-mcp --env-file /absolute/path/to/.env serve` to load a file
explicitly. Existing environment variables take precedence. See [.env.example](.env.example).

```sh
.venv/bin/telegram-mcp doctor  # Show paths and whether app credentials are available
.venv/bin/telegram-mcp serve   # stdio MCP; account tools open browser login as needed
```

Optional terminal login is retained for manual use. Configure the app credentials
first, then run `telegram-mcp login` for a terminal QR code or `login --phone` for
phone-number login. Run `telegram-mcp stop` first because terminal login opens the
same TDLib database directly. The backend restarts automatically on the next MCP connection.

## Connect an MCP client

Use your client's stdio MCP configuration with an absolute executable path.
On macOS, the bundled TDLib needs no environment configuration:

```json
{
  "mcpServers": {
    "telegram": {
      "command": "/absolute/path/to/telegram-mcp/.venv/bin/telegram-mcp",
      "args": ["serve"]
    }
  }
}
```

On other platforms or for a custom build, set `TDLIB_LIBRARY_PATH` in the client's
`env` configuration to the absolute library path. GUI apps generally do not inherit
variables exported in a terminal. Use the same `TELEGRAM_DATA_DIR` and
`TELEGRAM_DATABASE_KEY` as login if you set them.
Configuration format is client-specific; the process/args/env values above are the
same for any stdio client. Each stdio process forwards to a shared MCP backend on
`127.0.0.1`, authenticated by a random token stored with owner-only permissions.
The backend starts automatically, owns the single TDLib database connection, and
stays available when individual chats disconnect. After **5 minutes without MCP
requests**, it shuts down gracefully, closes TDLib, and preserves your login.
Set `TELEGRAM_IDLE_TIMEOUT` in seconds to change this duration; `0` disables idle
shutdown. Active requests, including browser login, finish before the idle countdown
starts. Health checks and open event streams do not keep the backend alive.
The lightweight stdio proxies reconnect and restart it automatically on the next
request, including requests from an already-open chat. Browser login uses a separate local page.
Use `telegram-mcp stop` to stop the backend gracefully. After updating server code,
stop the backend and restart the MCP connection in Codex to load the changes.

## Tools

Account data tools carry MCP read-only annotations and use an explicit allowlist
of TDLib methods. They automatically start browser authentication if needed.
The `logout` tool changes authentication state and is annotated accordingly.
There is no raw TDLib request tool.

| Tool | Purpose |
| --- | --- |
| `telegram_status` | Check whether the saved session is authorized |
| `logout` | Revoke the shared Telegram session and clear TDLib's local account cache |
| `get_me` | Current user's profile |
| `get_user` | Profile for a message sender's user ID |
| `list_chats` | First main/archive chats with unread counts and last messages |
| `search_chats` | Find account chats by title/username using server-backed search |
| `resolve_username` | Resolve a public @username without joining |
| `get_chat` | Chat details and last message |
| `list_topics` | Sidebar topics in forum groups and private bot chats, with name search and paging |
| `get_history` | Newest-first chat or sidebar topic history; pass `forum_topic_id` from `list_topics` |
| `get_message` | One message by TDLib IDs |
| `search_messages` | Global or chat-specific text search with paging |

`logout` signs out all MCP chats sharing the same data directory, while other Telegram
devices stay signed in. App credentials are kept. Logout does not open a browser;
the next account read starts the browser login flow again.

There are no send, edit, delete, reaction, join, read-receipt, or presence tools.
Reading history does not call `viewMessages` or mark messages as read. Authentication
and session/cache maintenance still communicate with Telegram and write local data.
Secret chats are disabled. Media metadata is included in messages; media download,
transcription, and full-text indexing of attachments are not implemented.

Limits are 1–100. TDLib may return a short history/chat batch while populating its
cache; a short batch alone is not proof that history is exhausted. `get_history`
uses an exclusive `before_message_id`; search uses the exact cursors returned by
TDLib. Message IDs are TDLib IDs, not the message numbers in `t.me` URLs.
Text and captions remain TDLib `formattedText` objects so formatting is preserved.

## Development

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

Tests use a deterministic TDLib transport and the real MCP SDK/client, without a
Telegram account. Browser tests cover app credential entry and persistence, QR images
and rotation, two-step verification, concurrent account calls, cancellation, timeouts,
and resuming the original read after login. Terminal phone/QR login is also tested.
Native-library tests use the bundled macOS library automatically, or a custom
library when `TDLIB_LIBRARY_PATH` is set:
they create/close a real TDLib client and connect to the server over subprocess
stdio without logging in, including concurrent connections using legacy and modern
MCP protocols. A live QR scan and account reads require real credentials.

The transport has one receive thread and correlates concurrent requests with
`@extra`. Authorization updates are handled independently of responses. Shutdown
waits for `authorizationStateClosed` so TDLib can flush its database. Diagnostics
go to stderr; stdout is reserved for MCP JSON while serving.

References: [TDLib JSON C interface](https://core.telegram.org/tdlib/docs/td__json__client_8h.html),
[TDLib getting started](https://core.telegram.org/tdlib/getting-started),
[TDLib source and build instructions](https://github.com/tdlib/td),
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
