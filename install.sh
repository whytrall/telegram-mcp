#!/usr/bin/env bash
set -euo pipefail

repository="whytrall/telegram-mcp"
install_dir="${TRL_TELEGRAM_MCP_DIR:-$HOME/.local/trl-telegram-mcp}"
command -v git >/dev/null || { echo "Install Git first." >&2; exit 1; }

if [[ -e "$install_dir" ]]; then
  if [[ ! -e "$install_dir/.git" ]]; then
    echo "$install_dir already exists and is not a Git checkout." >&2
    exit 1
  fi
  origin="$(git -C "$install_dir" remote get-url origin)"
  case "$origin" in
    "https://github.com/$repository"|"https://github.com/$repository.git"|"git@github.com:$repository.git") ;;
    *) echo "$install_dir belongs to another repository; refusing to modify it." >&2; exit 1 ;;
  esac
  if [[ "$(git -C "$install_dir" branch --show-current)" != master ]] || \
      [[ -n "$(git -C "$install_dir" status --porcelain)" ]]; then
    echo "Keep $install_dir on master with no local changes before updating." >&2
    exit 1
  fi
  git -C "$install_dir" pull --ff-only origin master </dev/null
else
  mkdir -p "$(dirname "$install_dir")"
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    gh repo clone "$repository" "$install_dir" -- --depth 1 --branch master </dev/null
  else
    git clone --depth 1 --branch master "https://github.com/$repository.git" "$install_dir" </dev/null
  fi
fi

exec bash "$install_dir/scripts/install.sh" "$@" </dev/null
