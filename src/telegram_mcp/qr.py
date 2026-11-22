from __future__ import annotations

import io
import sys
from typing import TextIO

import qrcode

QR_START_STATES = frozenset(
    {
        "authorizationStateWaitPhoneNumber",
        "authorizationStateWaitCode",
        "authorizationStateWaitEmailAddress",
        "authorizationStateWaitEmailCode",
        "authorizationStateWaitPremiumPurchase",
        "authorizationStateWaitRegistration",
    }
)


def qr_ascii(link: str, *, invert: bool = False) -> str:
    """Encode a TDLib login link as monospace text, with its quiet zone intact."""
    code = qrcode.QRCode(border=4)
    code.add_data(link)
    code.make(fit=True)
    buffer = io.StringIO()
    code.print_ascii(out=buffer, invert=invert)
    return buffer.getvalue()


class TerminalQR:
    """Render the current TDLib login token locally, replacing expired QR codes."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self._lines = 0

    def clear(self) -> None:
        if self._lines and self.stream.isatty():
            self.stream.write(f"\x1b[{self._lines}F\x1b[J")
            self.stream.flush()
        self._lines = 0

    def render(self, link: str) -> None:
        self.clear()
        rows = qr_ascii(link).splitlines()
        print(
            "In Telegram: Settings → Devices → Link Desktop Device. Scan this code:",
            file=self.stream,
        )
        for row in rows:
            # Fixed black on white preserves contrast on light and dark terminals.
            if self.stream.isatty():
                self.stream.write(f"\x1b[30;47m{row}\x1b[0m\n")
            else:
                self.stream.write(row + "\n")
        self.stream.flush()
        self._lines = len(rows) + 1
