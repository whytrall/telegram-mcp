"""Register the installed launcher while preserving unrelated client settings."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import tomlkit


def preserved_settings(previous) -> dict:
    """Updates must never silently remove environment-based chat restrictions."""
    if not isinstance(previous, Mapping):
        return {}
    result = {}
    for key in ("env_vars", "cwd"):
        if key in previous:
            result[key] = previous[key]
    if "env" in previous:
        env = previous["env"]
        if not isinstance(env, Mapping) or any(not isinstance(v, str) for v in env.values()):
            raise ValueError("The existing telegram environment must contain string values.")
        result["env"] = dict(env)
    args = previous.get("args", [])
    if isinstance(args, Sequence) and not isinstance(args, (str, bytes)):
        if any(
            isinstance(arg, str) and (arg == "--env-file" or arg.startswith("--env-file="))
            for arg in args
        ):
            result["args"] = list(args)
    return result


def write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure(launcher: Path, claude_path: Path, codex_path: Path) -> None:
    launcher = launcher.expanduser().resolve(strict=True)
    claude_path = claude_path.expanduser().resolve()
    codex_path = codex_path.expanduser().resolve()
    claude = json.loads(claude_path.read_text()) if claude_path.exists() else {}
    if not isinstance(claude, dict) or not isinstance(claude.get("mcpServers", {}), dict):
        raise ValueError(f"Expected a JSON object with an mcpServers object in {claude_path}")
    codex = tomlkit.parse(codex_path.read_text()) if codex_path.exists() else tomlkit.document()
    command = {"command": str(launcher), "args": ["serve"]}
    claude_servers = claude.setdefault("mcpServers", {})
    codex_servers = codex.setdefault("mcp_servers", {})
    claude_settings = preserved_settings(claude_servers.get("telegram"))
    codex_settings = preserved_settings(codex_servers.get("telegram"))
    claude_servers["telegram"] = {"type": "stdio", **command, **claude_settings}
    codex_servers["telegram"] = {
        **command,
        **codex_settings,
        "startup_timeout_sec": 45,
        "tool_timeout_sec": 360,
    }
    # Parse and render both configurations before writing either file.
    claude_text = json.dumps(claude, ensure_ascii=False, indent=2) + "\n"
    codex_text = tomlkit.dumps(codex)
    write_config(claude_path, claude_text)
    write_config(codex_path, codex_text)
    print(f"Registered telegram in {claude_path} and {codex_path}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launcher", type=Path)
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument(
        "--codex-config",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml",
    )
    args = parser.parse_args()
    try:
        configure(args.launcher, args.claude_config, args.codex_config)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Unable to configure MCP clients: {exc}\n")


if __name__ == "__main__":
    main()
