# Telegram MCP

An MCP server for Telegram based on TDLib, with read tools and confirmed message sending

Important note: you should be careful when using third-party Telegram clients, 
including this one. Check the source code before signing in, or, in current realities, 
ask your AI agent to check the code to make sure that I'm not trying to steal your messages

No guarantees: this MCP server works for me, so it *might* work for you

## Installation

```sh
curl -fsSL https://raw.githubusercontent.com/whytrall/telegram-mcp/refs/heads/master/install.sh | bash
```

Requires **Python 3.11+**, Git, and curl

Or, even a lazier version, paste this to your agent:

```
Install MCP server from https://github.com/whytrall/telegram-mcp
```

## Tools

Read tools carry MCP read-only annotations and use an explicit allowlist
of TDLib methods. They automatically start browser authentication if needed.
The `logout` tool changes authentication state and is annotated accordingly.
`send_message` sends text, photos, videos, and files. **Do not use it without explicit
user confirmation** of the recipient, content, and attachments; it requires `confirmed=true`.
Attachments use an absolute local `path` and `type` (`photo`, `video`, or `file`).
Up to 10 photos/videos or 10 files can be grouped; files and photos/videos cannot mix.
Text is the first attachment's caption, or a standalone message when no attachments are given.
Videos must already be MP4/H.264. A `pending` result means upload/delivery is still
in progress. Check message history before retrying an uncertain send to avoid duplicates.
Pending sends keep the backend alive until TDLib reports success or failure, so idle
shutdown does not interrupt an upload.
There is no raw TDLib request tool.

| Tool | Purpose |
| --- | --- |
| `telegram_status` | Check whether the saved session is authorized |
| `logout` | Revoke the shared Telegram session and clear TDLib's local account cache |
| `send_message` | Send confirmed text, photos, videos, or files; supports topics and replies |
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


## Details

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
