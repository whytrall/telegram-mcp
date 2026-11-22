from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from .client import TDLibClient, TDLibError
from .config import Config
from .qr import QR_START_STATES, TerminalQR


async def prompt(label: str, *, secret: bool = False) -> str:
    read: Callable[[str], str] = getpass.getpass if secret else input
    value = await asyncio.to_thread(read, label)
    return value if secret else value.strip()


async def login(config: Config, method: Literal["qr", "phone"] = "qr") -> None:
    display = TerminalQR()
    qr_requested = False
    try:
        async with TDLibClient(config) as client:
            while not client.ready:
                state = client.authorization_state
                kind = state["@type"]
                if kind != "authorizationStateWaitOtherDeviceConfirmation":
                    display.clear()
                if method == "qr" and not qr_requested and kind in QR_START_STATES:
                    try:
                        await client.request("requestQrCodeAuthentication", other_user_ids=[])
                    except TDLibError as exc:
                        raise RuntimeError(
                            f"QR login failed: {exc}. "
                            "Retry later or use `telegram-mcp login --phone`."
                        ) from None
                    qr_requested = True
                    await client.wait_for_auth_change(state)
                    continue
                try:
                    if kind == "authorizationStateWaitPhoneNumber":
                        if method == "qr":
                            raise RuntimeError("QR login ended. Run `telegram-mcp login` again.")
                        await client.request(
                            "setAuthenticationPhoneNumber",
                            phone_number=await prompt("Phone number (international format): "),
                        )
                    elif kind == "authorizationStateWaitCode":
                        delivery = (
                            state.get("code_info", {}).get("type", {}).get("@type", "Telegram")
                        )
                        print(f"Code delivery: {delivery}", file=sys.stderr)
                        await client.request(
                            "checkAuthenticationCode",
                            code=await prompt("Login code: ", secret=True),
                        )
                    elif kind == "authorizationStateWaitPassword":
                        await client.request(
                            "checkAuthenticationPassword",
                            password=await prompt("Two-step verification password: ", secret=True),
                        )
                    elif kind == "authorizationStateWaitEmailAddress":
                        await client.request(
                            "setAuthenticationEmailAddress",
                            email_address=await prompt("Login email: "),
                        )
                    elif kind == "authorizationStateWaitEmailCode":
                        await client.request(
                            "checkAuthenticationEmailCode",
                            code={
                                "@type": "emailAddressAuthenticationCode",
                                "code": await prompt("Email login code: ", secret=True),
                            },
                        )
                    elif kind == "authorizationStateWaitOtherDeviceConfirmation":
                        # TDLib emits a new link automatically when the token expires.
                        display.render(state["link"])
                    else:
                        raise RuntimeError(
                            f"Login requires {kind}. Use an official app to finish this step."
                        )
                except TDLibError as exc:
                    print(str(exc), file=sys.stderr)
                    continue
                await client.wait_for_auth_change(state)
            display.clear()
            print("Logged in. The session is saved; you can now start `telegram-mcp serve`.")
    finally:
        display.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Telegram MCP server backed by TDLib.")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Load configuration from this explicit .env file; existing environment values win.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Run the read-only stdio MCP server (default).")
    commands.add_parser("serve-backend", help="Run the shared local Telegram backend.")
    commands.add_parser("stop", help="Stop the shared Telegram backend.")
    login_parser = commands.add_parser("login", help="Log in from the terminal using a QR code.")
    methods = login_parser.add_mutually_exclusive_group()
    methods.add_argument(
        "--qr",
        dest="method",
        action="store_const",
        const="qr",
        help="Scan a QR code from an existing Telegram session (default).",
    )
    methods.add_argument(
        "--phone",
        dest="method",
        action="store_const",
        const="phone",
        help="Log in using a phone number and authentication code instead.",
    )
    login_parser.set_defaults(method="qr")
    commands.add_parser("doctor", help="Show local configuration paths.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        if args.env_file is not None:
            if not args.env_file.is_file():
                raise ValueError("The specified --env-file does not exist.")
            load_dotenv(args.env_file, override=False, interpolate=False)
        config = Config.from_env().load_credentials()
        if args.command == "login":
            if not config.has_credentials:
                raise ValueError(
                    "For terminal login, configure TELEGRAM_API_ID and TELEGRAM_API_HASH. "
                    "Or start the MCP server and call an account tool "
                    "to sign in through the browser."
                )
            if not sys.stdin.isatty():
                raise RuntimeError(
                    "Run login in an interactive terminal; MCP stdio is reserved for JSON."
                )
            asyncio.run(login(config, method=args.method))
        elif args.command == "doctor":
            # Imports are delayed so `--help` needs neither credentials nor TDLib.
            from .native import NativeTDLib

            print(
                json.dumps(
                    {
                        "python": sys.version.split()[0],
                        "library": config.library_path or NativeTDLib.find_library(),
                        "data_directory": str(config.data_dir),
                        "credentials_configured": config.has_credentials,
                        "session_database_present": (
                            config.data_dir / "database" / "td.binlog"
                        ).exists(),
                        "read_only": True,
                    },
                    indent=2,
                )
            )
        elif args.command == "serve-backend":
            from .backend import run_backend

            run_backend(config)
        elif args.command == "stop":
            from .backend import stop_backend

            print("Stopped." if stop_backend(config) else "Backend is not running.")
        else:
            from .proxy import run_proxy

            asyncio.run(run_proxy(config))
    except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
        print(f"telegram-mcp: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        print("Stopped.", file=sys.stderr)
        raise SystemExit(130) from None
