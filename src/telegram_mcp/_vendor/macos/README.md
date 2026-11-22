# Prebuilt TDLib for macOS

`libtdjson.dylib` is a universal2 binary for Apple Silicon (`arm64`) and Intel
(`x86_64`), built with a macOS 13.0 deployment target. It includes OpenSSL
statically and dynamically links only Apple's system libraries. No Homebrew
installation is required to load it.

The binary is ad-hoc signed. `manifest.json` records its source versions,
architectures, and SHA-256. The server discovers it relative to the installed
Python package; `TDLIB_LIBRARY_PATH` takes precedence when explicitly configured.

Rebuild from the repository root:

```sh
brew install cmake gperf
bash scripts/prebuild_tdlib_macos.sh
```

The script verifies the pinned OpenSSL source archive, builds both architectures,
checks that their OpenSSL headers agree, and rejects non-system dynamic library
dependencies. Downloads and intermediate build products are kept in `.deps/`.
The resulting binary, manifest, and notices belong in the repository and are
included in the Python wheel.

Bundled components:

- [TDLib](https://github.com/tdlib/td), including its utility libraries:
  copyright Aliaksei Levin and Arseny Smirnov, 2014–2026;
  Boost Software License 1.0, reproduced in `LICENSE.tdlib`.
- [OpenSSL](https://github.com/openssl/openssl): Apache License 2.0,
  reproduced in `LICENSE.openssl`; authors listed in `AUTHORS.openssl`.
- TDLib's bundled SQLite with encryption extensions: the SQLite core is public
  domain; the extension license is reproduced in `LICENSE.sqlite`.

This binary was compiled on a newer macOS version. The deployment target is
13.0; running it on macOS 13 itself requires a separate compatibility check.
