"""A status line for work in progress.

While Pulley resolves, downloads, and installs, one line on standard error says what it is
doing (with a spinner, and bytes for downloads), and is erased when the next output or the
end of the command comes. It shows only on a terminal and not with --quiet, so logs and
pipes get only the lasting lines Pulley prints.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
INTERVAL = 0.1


def size(n: float) -> str:
    """A byte count for people: 512 B, 12.3 KB, 4.0 MB."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class Progress:
    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.enabled = False
        self._text: str | None = None
        self._detail = ""
        self._frame = 0
        self._shown = False
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def enable(self, on: bool | None = None) -> None:
        """Turns the line on (by default, when standard error is a terminal)."""
        if on is None:
            try:
                on = self.stream.isatty() and os.environ.get("TERM") != "dumb"
            except (AttributeError, ValueError):
                on = False
        self.enabled = bool(on)

    def status(self, text: str) -> None:
        """Says what Pulley is doing now, replacing what it said before."""
        if not self.enabled:
            return
        with self._lock:
            self._text, self._detail = text, ""
            self._draw()
        self._start()

    def transfer(self, done: int, total: int = 0) -> None:
        """Bytes so far of the download the status names."""
        if not self.enabled or self._text is None:
            return
        with self._lock:
            if total:
                self._detail = f"{size(done)} / {size(total)} ({min(100, done * 100 // total)}%)"
            else:
                self._detail = size(done)

    def clear(self) -> None:
        """Erases the line (before other output) and forgets it until the next status."""
        with self._lock:
            self._text = None
            self._erase()

    def _erase(self) -> None:
        if self._shown:
            self.stream.write("\r\033[K")
            self.stream.flush()
            self._shown = False

    def _draw(self) -> None:
        if self._text is None:
            return
        width = shutil.get_terminal_size((80, 24)).columns - 1
        line = f"{SPINNER[self._frame % len(SPINNER)]} {self._text}" + (f"  {self._detail}" if self._detail else "")
        if len(line) > width:
            line = line[: max(0, width - 1)] + "…"
        self.stream.write("\r\033[K\033[36m" + line + "\033[0m")
        self.stream.flush()
        self._shown = True

    def _start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._spin, name="pulley-progress", daemon=True)
            self._thread.start()

    def _spin(self) -> None:
        while True:
            time.sleep(INTERVAL)
            with self._lock:
                if self._text is not None:
                    self._frame += 1
                    self._draw()


progress = Progress()
