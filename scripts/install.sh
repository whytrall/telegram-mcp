#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
installer_python=""
for candidate in python3 python3.13 python3.12 python3.11; do
  if command -v "$candidate" >/dev/null && \
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    installer_python="$candidate"
    break
  fi
done
if [[ -z "$installer_python" ]]; then
  echo "Install Python 3.11 or newer, then rerun this script." >&2
  exit 1
fi

"$installer_python" -m venv "$root/.venv"
"$root/.venv/bin/python" -m pip install "$root[install]"
"$root/.venv/bin/python" "$root/scripts/configure_clients.py" "$root/.venv/bin/telegram-mcp" "$@"
echo "Installed in $root. Restart Claude Code and Codex to load Telegram tools."
echo "Your first account read opens the browser login page."
