#!/usr/bin/env bash
set -euo pipefail

# Pin the JSON schema and native ABI tested with this server. Override deliberately
# with TDLIB_REF when upgrading; TDLib's old 1.8.0 release has a different auth API.
ref="${TDLIB_REF:-03220f6b3043c6fd7785fbf0901d63f19ad19974}"
root="$(cd "$(dirname "$0")/.." && pwd)"
deps="${TDLIB_BUILD_DIR:-$root/.deps}"
jobs="${TDLIB_BUILD_JOBS:-4}"

command -v cmake >/dev/null || { echo "Install CMake first (see README.md)." >&2; exit 1; }
command -v gperf >/dev/null || { echo "Install gperf first (see README.md)." >&2; exit 1; }
mkdir -p "$deps"
if [[ ! -d "$deps/td/.git" ]]; then
  git init "$deps/td"
  git -C "$deps/td" remote add origin https://github.com/tdlib/td.git
fi
if [[ -n "$(git -C "$deps/td" status --porcelain)" ]]; then
  echo "TDLib checkout has local changes; refusing to overwrite them." >&2
  exit 1
fi
git -C "$deps/td" fetch --depth 1 origin "$ref"
git -C "$deps/td" checkout --detach FETCH_HEAD

extra=()
if [[ "$(uname -s)" == Darwin ]] && command -v brew >/dev/null; then
  extra+=("-DOPENSSL_ROOT_DIR=$(brew --prefix openssl@3)")
fi
cmake -S "$deps/td" -B "$deps/td-build" \
  -DCMAKE_BUILD_TYPE=Release -DTD_ENABLE_JNI=OFF -DTD_ENABLE_DOTNET=OFF "${extra[@]}" "$@"
cmake --build "$deps/td-build" --target tdjson --parallel "$jobs"
echo "TDLib built. Set TDLIB_LIBRARY_PATH to $deps/td-build/libtdjson.dylib (macOS) or libtdjson.so (Linux)."
