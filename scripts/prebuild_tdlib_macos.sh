#!/usr/bin/env bash
set -euo pipefail

# Build one relocatable universal2 library. Only Apple's system libraries remain
# dynamically linked; OpenSSL is built from verified source and linked statically.
root="$(cd "$(dirname "$0")/.." && pwd)"
deps="${TDLIB_PREBUILD_DIR:-$root/.deps/macos-prebuilt}"
jobs="${TDLIB_BUILD_JOBS:-8}"
target=13.0
openssl_version=3.5.9
openssl_sha256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
archive="$deps/openssl-$openssl_version.tar.gz"
source_dir="$deps/openssl-$openssl_version"
universal="$deps/openssl-universal"
output="$root/src/telegram_mcp/_vendor/macos"

[[ "$(uname -s)" == Darwin ]] || { echo "Run this script on macOS." >&2; exit 1; }
for tool in cmake gperf perl clang curl tar shasum make lipo strip install_name_tool codesign python3; do
  command -v "$tool" >/dev/null || { echo "Missing build tool: $tool" >&2; exit 1; }
done
mkdir -p "$deps" "$universal/lib" "$output"
if [[ ! -f "$archive" ]]; then
  curl --fail --location --retry 3 \
    "https://github.com/openssl/openssl/releases/download/openssl-$openssl_version/openssl-$openssl_version.tar.gz" \
    --output "$archive.part"
  mv "$archive.part" "$archive"
fi
echo "$openssl_sha256  $archive" | shasum -a 256 --check
if [[ ! -d "$source_dir" ]]; then
  tar -xzf "$archive" -C "$deps"
fi

for arch in arm64 x86_64; do
  openssl_build="$deps/openssl-build-$arch"
  prefix="$deps/openssl-$arch"
  mkdir -p "$openssl_build"
  (
    cd "$openssl_build"
    if [[ ! -f Makefile ]]; then
      perl "$source_dir/Configure" "darwin64-$arch-cc" \
        no-shared no-tests no-module no-dso \
        --prefix="$prefix" --libdir=lib --openssldir=/etc/ssl \
        "-mmacosx-version-min=$target"
    fi
    make -j "$jobs" build_libs
    make install_dev
  )
done

# OpenSSL's public configuration is the same for these two little-endian LP64
# targets. Refuse to accidentally ship incompatible headers after an upgrade.
diff -r "$deps/openssl-arm64/include" "$deps/openssl-x86_64/include"
cp -R "$deps/openssl-arm64/include" "$universal/"
for library in libcrypto.a libssl.a; do
  lipo -create "$deps/openssl-arm64/lib/$library" "$deps/openssl-x86_64/lib/$library" \
    -output "$universal/lib/$library"
done

TDLIB_BUILD_DIR="$deps" TDLIB_BUILD_JOBS="$jobs" bash "$root/scripts/build_tdlib.sh" \
  '-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64' \
  "-DCMAKE_OSX_DEPLOYMENT_TARGET=$target" \
  -DOPENSSL_USE_STATIC_LIBS=TRUE \
  "-DOPENSSL_ROOT_DIR=$universal" \
  "-DOPENSSL_INCLUDE_DIR=$universal/include" \
  "-DOPENSSL_CRYPTO_LIBRARY=$universal/lib/libcrypto.a" \
  "-DOPENSSL_SSL_LIBRARY=$universal/lib/libssl.a"

cp -L "$deps/td-build/libtdjson.dylib" "$output/libtdjson.dylib"
install_name_tool -id @rpath/libtdjson.dylib "$output/libtdjson.dylib"
strip -x "$output/libtdjson.dylib"
codesign --force --sign - "$output/libtdjson.dylib"
cp "$deps/td/LICENSE_1_0.txt" "$output/LICENSE.tdlib"
cp "$deps/td/sqlite/sqlite/LICENSE" "$output/LICENSE.sqlite"
cp "$source_dir/LICENSE.txt" "$output/LICENSE.openssl"
cp "$source_dir/AUTHORS.md" "$output/AUTHORS.openssl"

python3 - "$output" "$deps/td" "$openssl_version" "$target" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path

output, td_source = map(Path, sys.argv[1:3])
library = output / "libtdjson.dylib"
architectures = subprocess.check_output(["lipo", "-archs", str(library)], text=True).split()
assert set(architectures) == {"arm64", "x86_64"}, architectures
for architecture in architectures:
    dependencies = subprocess.check_output(["otool", "-arch", architecture, "-L", str(library)], text=True)
    for line in dependencies.splitlines()[1:]:
        name = line.strip().split(" (", 1)[0]
        if not name.startswith(("@rpath/libtdjson.dylib", "/usr/lib/", "/System/Library/")):
            raise SystemExit(f"Library has a non-system dependency: {name}")
manifest = {
    "tdlib_commit": subprocess.check_output(["git", "-C", str(td_source), "rev-parse", "HEAD"], text=True).strip(),
    "openssl_version": sys.argv[3],
    "minimum_macos": sys.argv[4],
    "architectures": architectures,
    "sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
    "build_command": "bash scripts/prebuild_tdlib_macos.sh",
}
(output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Prebuilt TDLib: {library} ({library.stat().st_size / 1024**2:.1f} MiB)")
PY
