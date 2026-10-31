"""Команды терминала читаются отдельно; Playwright остаётся в основном потоке."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Callable, TextIO


LOG = logging.getLogger("hh_browser")


@dataclass
class CycleProgress:
    """Общий лимит и просмотренные вакансии для всех поисков одного цикла."""

    max_vacancies: int
    max_applications: int
    checked: int = 0
    sent: int = 0
    seen_ids: set[str] = field(default_factory=set)

    @property
    def full(self):
        return self.checked >= self.max_vacancies or self.sent >= self.max_applications


class CycleExpired(RuntimeError):
    """Пора закончить текущую четырёхчасовую пачку."""


class RunStopped(RuntimeError):
    pass


class RunControl:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.now = clock
        self._condition = threading.Condition()
        self._paused = False
        self._stopped = False
        self._manual_waiting = False
        self._manual_confirmed = False
        self._console_closed = False
        self._maintenance = None
        self._maintenance_running = False

    @contextmanager
    def maintenance(self, callback):
        previous = self._maintenance
        self._maintenance = (threading.get_ident(), callback)
        try:
            yield
        finally:
            self._maintenance = previous

    def _poll_maintenance(self):
        if self._maintenance and not self._maintenance_running:
            owner, callback = self._maintenance
            if owner != threading.get_ident():
                return
            self._maintenance_running = True
            try:
                callback()
            finally:
                self._maintenance_running = False

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    def handle_command(self, command: str) -> None:
        command = command.strip()
        with self._condition:
            if not command and self._manual_waiting:
                self._manual_confirmed = True
            elif command == "INeedSomeSleep":
                self._paused = True
                LOG.info("Пауза запрошена. Текущая операция завершится; новые действия остановлены. "
                         "Для продолжения введите WhyYouSoUgly и Enter.")
            elif command == "WhyYouSoUgly":
                self._paused = False
                LOG.info("Работа продолжена.")
            elif command:
                LOG.info("Неизвестная команда. Пауза: INeedSomeSleep; продолжение: WhyYouSoUgly; выход: Ctrl+C.")
            self._condition.notify_all()

    def start_console(self, stream: TextIO | None = None) -> threading.Thread:
        stream = stream if stream is not None else sys.stdin

        def read_commands():
            try:
                if os.name == "nt" and stream is sys.stdin and stream.isatty():
                    # Неблокирующий ввод: при Ctrl+C не остаётся daemon-потока,
                    # удерживающего блокировку sys.stdin во время завершения Python.
                    import msvcrt
                    buffer = ""
                    while True:
                        with self._condition:
                            if self._stopped:
                                return
                        if not msvcrt.kbhit():
                            with self._condition:
                                self._condition.wait(timeout=0.05)
                            continue
                        char = msvcrt.getwch()
                        if char in ("\x00", "\xe0"):
                            msvcrt.getwch()
                        elif char in ("\r", "\n"):
                            print(flush=True)
                            self.handle_command(buffer)
                            buffer = ""
                        elif char == "\b":
                            if buffer:
                                buffer = buffer[:-1]
                                print("\b \b", end="", flush=True)
                        elif char == "\x03":
                            import _thread
                            _thread.interrupt_main()
                        elif char.isprintable():
                            buffer += char
                            print(char, end="", flush=True)
                    return
                for line in stream:
                    with self._condition:
                        if self._stopped:
                            return
                    self.handle_command(line)
            except (OSError, ValueError):
                LOG.warning("Ввод команд из терминала недоступен. Остановить процесс можно через Ctrl+C.")
            finally:
                with self._condition:
                    self._console_closed = True
                    self._condition.notify_all()

        thread = threading.Thread(target=read_commands, name="console-commands", daemon=True)
        thread.start()
        LOG.info("Команды + Enter: INeedSomeSleep — пауза; WhyYouSoUgly — продолжение; Ctrl+C — выход.")
        return thread

    def begin_manual_check(self) -> None:
        with self._condition:
            self._manual_waiting = True
            self._manual_confirmed = False

    def poll_manual_confirmation(self) -> bool:
        with self._condition:
            if self._stopped:
                raise RunStopped("Работа остановлена.")
            if self._manual_confirmed:
                self._manual_confirmed = False
                return True
            if self._console_closed:
                raise RuntimeError("Терминал закрыт: невозможно получить Enter после ручной проверки.")
            return False

    def end_manual_check(self) -> None:
        with self._condition:
            self._manual_waiting = self._manual_confirmed = False

    def checkpoint(self, deadline: float | None = None) -> None:
        with self._condition:
            while self._paused and not self._stopped:
                self._condition.wait(timeout=0.25)
            if self._stopped:
                raise RunStopped("Работа остановлена.")
        if deadline is not None and self.now() >= deadline:
            raise CycleExpired("Четырёхчасовой цикл завершён.")
        self._poll_maintenance()
        if deadline is not None and self.now() >= deadline:
            raise CycleExpired("Четырёхчасовой цикл завершён.")

    def check_stopped(self) -> None:
        with self._condition:
            if self._stopped:
                raise RunStopped("Работа остановлена.")

    def wait_until(self, target: float, *, deadline: float | None = None) -> None:
        while True:
            self.checkpoint(deadline)
            remaining = target - self.now()
            if remaining <= 0:
                return
            with self._condition:
                self._condition.wait(timeout=min(remaining, 0.25))

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
