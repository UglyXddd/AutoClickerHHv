"""Новые поиски до лимита цикла; поднятие резюме по фактическим таймерам hh.ru."""

from __future__ import annotations

import logging
from pathlib import Path

from actions import load_config, run_actions
from runtime_control import CycleExpired, CycleProgress, RunControl, RunStopped
from openai_client import OpenAIError, OpenAIStop
from hh_ui import captcha_present
from response_guard import ACTIVE_RESPONSE_GUARD
from resume_bumps import ResumeBumpSchedule


LOG = logging.getLogger("hh_browser")


def run_continuous(page, *, task: str, config_path: Path, dry_run: bool, control: RunControl,
                   on_human_required=None, page_provider=None) -> None:
    # У пользовательских реализаций control поддержка обслуживания необязательна.
    if task not in {"all", "bump"} or not hasattr(control, "maintenance"):
        return _run_cycles(page, task=task, config_path=config_path, dry_run=dry_run, control=control,
                           on_human_required=on_human_required, page_provider=page_provider)

    maintenance_page = None

    def current_page():
        return page_provider() if page_provider else page

    def blocked():
        return ACTIVE_RESPONSE_GUARD.get() is not None or captcha_present(current_page())

    def check_resumes():
        nonlocal maintenance_page
        if maintenance_page is None or maintenance_page.is_closed():
            maintenance_page = current_page().context.new_page()
            LOG.info("Проверяю таймеры поднятия в отдельной вкладке того же браузера.")
        # Глобальный captcha_handler работает с переданной вкладкой; обработчик
        # восстановления основной страницы сюда не передаём.
        return run_actions(maintenance_page, task="bump", config_path=config_path,
                           dry_run=dry_run, control=control)

    schedule = ResumeBumpSchedule(control, check_resumes, blocked=blocked)
    try:
        with control.maintenance(schedule.poll):
            return _run_cycles(page, task=task, config_path=config_path, dry_run=dry_run, control=control,
                               on_human_required=on_human_required, page_provider=page_provider,
                               bump_schedule=schedule)
    finally:
        if maintenance_page is not None and not maintenance_page.is_closed():
            maintenance_page.close()


def _run_cycles(page, *, task, config_path, dry_run, control, on_human_required=None,
                page_provider=None, bump_schedule=None):
    cycle = 0
    try:
        while True:
            control.checkpoint()
            config = load_config(config_path)
            cycle += 1
            LOG.info("Начат цикл №%s: до %s вакансий за %s ч.; задача %s%s.",
                     cycle, config["max_vacancies"], config["cycle_hours"], task,
                     " (пробный запуск)" if dry_run else "")
            if task in {"all", "bump"}:
                delay = run_actions(page_provider() if page_provider else page, task="bump", config_path=config_path,
                                    dry_run=dry_run, control=control, on_human_required=on_human_required)
                if bump_schedule:
                    bump_schedule.observe(delay)
            # Четыре часа отсчитываются после завершения поднятия всех резюме.
            # Иначе следующая попытка могла бы прийти на несколько секунд раньше таймера hh.ru.
            started = control.now()
            deadline = started + config["cycle_hours"] * 3600
            progress = CycleProgress(config["max_vacancies"], config.get("max_applications", config["max_vacancies"]))
            try:
                if task != "bump":
                    while control.now() < deadline and not progress.full:
                        control.checkpoint()
                        try:
                            found = run_actions(page_provider() if page_provider else page, task="apply" if task == "all" else task,
                                                config_path=config_path, dry_run=dry_run,
                                                control=control, cycle_started=started, deadline=deadline,
                                                on_human_required=on_human_required, progress=progress)
                        except OpenAIStop:
                            raise
                        except OpenAIError as exc:
                            LOG.error("Не удалось создать следующий поиск: %s", exc)
                            found = 0
                        if progress.full or control.now() >= deadline:
                            break
                        if not found:
                            delay = config.get("empty_search_delay_seconds", 60)
                            LOG.info("Новых вакансий в поиске нет. Следующий запрос через %s с.", delay)
                            control.wait_until(min(control.now() + delay, deadline))
                        else:
                            LOG.info("Поиск обработан. Создаю следующий запрос; в цикле проверено %s/%s, откликов %s/%s.",
                                     progress.checked, progress.max_vacancies, progress.sent, progress.max_applications)
            except CycleExpired:
                LOG.info("Время цикла №%s истекло. Новые отклики из этой пачки не начинаю.", cycle)
            remaining = max(0, deadline - control.now())
            if remaining:
                LOG.info("До следующего цикла %.1f мин. Команды паузы доступны.", remaining / 60)
                control.wait_until(deadline)
    except RunStopped:
        LOG.info("Постоянная работа остановлена.")
