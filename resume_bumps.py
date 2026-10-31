"""Чтение таймера hh.ru и независимое от поискового цикла расписание поднятия."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from playwright.sync_api import Error as PlaywrightError

from hh_ui import HumanRequired, SessionRequired
from runtime_control import CycleExpired, RunStopped


MOSCOW = timezone(timedelta(hours=3), "Europe/Moscow")
LOG = logging.getLogger("hh_browser")


def next_bump_delay(text, *, now=None):
    """Возвращает секунды до времени в карточке; неизвестный формат — None."""
    now = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    text = " ".join(text.casefold().split())
    marker = re.search(r"(?:поднять|обновить).{0,65}?(?:можно|доступн[оа])", text)
    if not marker:
        return None
    timer = text[marker.end():marker.end() + 100]
    if "через" in timer:
        duration = timer.split("через", 1)[1]
        parts = re.findall(r"(\d+)\s*(день|дня|дней|дн\.?|часа?|часов|ч\.?|минут[ауы]?|мин\.?|секунд[ауы]?|сек\.?)\b", duration)
        if parts:
            seconds = 0
            for value, unit in parts:
                scale = 86400 if unit.startswith("д") else 3600 if unit.startswith("ч") else 60 if unit.startswith("м") else 1
                seconds += int(value) * scale
            return float(seconds)
        return None
    clock = re.search(r"\b(?:в\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?\b", timer)
    if not clock:
        return None
    hour, minute, second = (int(clock[1]), int(clock[2]), int(clock[3] or 0))
    try:
        target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if "послезавтра" in timer[:clock.start()]:
            target += timedelta(days=2)
        elif "завтра" in timer[:clock.start()]:
            target += timedelta(days=1)
        else:
            date = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b", timer[:clock.start()])
            if date:
                target = target.replace(year=int(date[3] or now.year), month=int(date[2]), day=int(date[1]))
        # Истёкшее время не переносим на завтра: нужна близкая повторная проверка.
        return max(0.0, (target - now).total_seconds())
    except ValueError:
        return None


class ResumeBumpSchedule:
    def __init__(self, control, check, *, blocked=lambda: False, retry_seconds=60):
        self.control, self.check, self.blocked = control, check, blocked
        self.retry_seconds = retry_seconds
        self.next_check = float("inf")

    def observe(self, delay):
        delay = self.retry_seconds if delay is None else max(5, float(delay))
        self.next_check = self.control.now() + delay

    def poll(self):
        if self.control.now() < self.next_check or self.blocked():
            return
        # Повтор не раньше интервала даже при исключении. Сама проверка работает
        # только из основного потока, внутри защищённого от рекурсии maintenance.
        self.observe(None)
        try:
            delay = self.check()
        except (RunStopped, CycleExpired, HumanRequired, SessionRequired):
            raise
        except (PlaywrightError, RuntimeError, AssertionError) as exc:
            LOG.error("Не удалось проверить поднятие резюме: %s. Следующая проверка через %s с.",
                      str(exc)[:300], self.retry_seconds)
            self.observe(None)
        else:
            self.observe(delay)
