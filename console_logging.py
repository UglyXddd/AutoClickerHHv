"""Цвета терминала; файлы журнала сохраняются обычным JSON без ANSI-кодов."""

from __future__ import annotations

import logging
import os
import sys


RESET = "\x1b[0m"
ORANGE = "\x1b[38;2;255;165;0m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
EVENT_COLORS = {
    "not_suitable": ORANGE,
    "requirements_missing": ORANGE,
    "applied": GREEN,
    "questionnaire": YELLOW,
    "ui_error": RED,
    "vacancy_error": RED,
    "model_error": RED,
    "unsupported_response": RED,
    "response_blocked": RED,
    "submission_unknown": RED,
    "bump_unconfirmed": RED,
}


def enable_terminal_colors(stream) -> bool:
    if not getattr(stream, "isatty", lambda: False)():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.SetConsoleMode.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = wintypes.DWORD()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (OSError, ValueError, AttributeError):
        pass
    # Терминалы с собственным PTY могут не предоставлять Win32 console handle.
    return os.environ.get("TERM", "").startswith(("xterm", "screen"))


class ConsoleFormatter(logging.Formatter):
    def __init__(self, *, colors: bool):
        super().__init__("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
        self.colors = colors

    def format(self, record):
        message = super().format(record)
        if not self.colors:
            return message
        color = EVENT_COLORS.get(getattr(record, "hh_event", ""))
        if color is None and record.levelno >= logging.WARNING:
            color = RED
        return color + message + RESET if color else message


def configure_console_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ConsoleFormatter(colors=enable_terminal_colors(sys.stderr)))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
