"""Ручной вход и автоматическая работа на hh.ru в видимом браузере."""

from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

try:
    from playwright.sync_api import (
        BrowserContext,
        Error as PlaywrightError,
        Page,
        Playwright,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Установите зависимости: python -m pip install -r requirements.txt"
    ) from exc

from actions import run_actions
from hh_ui import HumanRequired, SessionRequired, captcha_handler, check_challenge, navigate
from runtime_control import RunControl
from service import run_continuous
from console_logging import configure_console_logging
from captcha_relay import CaptchaRelay
from telegram_captcha import TelegramCaptcha
from actions import load_config
from openai_client import load_api_key, validate_config


LOGIN_URL = "https://hh.ru/account/login?backurl=%2Fapplicant%2Fprofile%2Fme"
ACCOUNT_URL = "https://hh.ru/applicant/profile/me?hhtmFrom=services"

# Положительный признак входа. При изменении вёрстки hh.ru обновите селекторы.
SIGNED_IN_SELECTOR = ", ".join(
    (
        '[data-qa^="mainmenu_applicantProfile"]',
        '[data-qa="mainmenu_myResumes"]',
        'a[href*="/account/logout"]',
    )
)
LOG = logging.getLogger("hh_browser")


@dataclass(frozen=True)
class Settings:
    profile: Path
    browser: str = "msedge" if os.name == "nt" else "chromium"
    timeout_ms: int = 30_000
    task: str = "bump"
    config_path: Path = Path(__file__).with_name("config.json")
    dry_run: bool = False
    continuous: bool = False
    console_control: bool = False


def default_profile(browser: str) -> Path:
    # На Windows профиль остаётся вне OneDrive и папки с исходниками.
    if os.name == "nt":
        data_home = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "hh-browser" / browser / "profile"


@contextmanager
def open_browser(
    playwright: Playwright, settings: Settings, *, headless: bool
) -> Iterator[BrowserContext]:
    settings.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(settings.profile),
        channel=settings.browser,
        headless=headless,
        locale="ru-RU",
        viewport={"width": 1440, "height": 1000},
        timeout=settings.timeout_ms,
    )
    context.set_default_timeout(settings.timeout_ms)
    context.set_default_navigation_timeout(settings.timeout_ms)
    restored = False
    try:
        # Chromium может удалить session cookies при штатном закрытии окна.
        # Постоянные cookies и остальные хранилища сохраняет сам профиль.
        cookies_path = settings.profile / "session-cookies.json"
        if cookies_path.exists():
            try:
                cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise RuntimeError(
                    "Не удалось прочитать session-cookies.json в папке профиля. "
                    "Переместите этот файл и повторите login."
                ) from exc
            context.add_cookies(cookies)
        restored = True
        yield context
    finally:
        try:
            if restored and context.browser and context.browser.is_connected():
                session_cookies = [
                    cookie for cookie in context.cookies() if cookie.get("expires", -1) < 0
                ]
                temporary = settings.profile / "session-cookies.json.tmp"
                with os.fdopen(
                    os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600),
                    "w", encoding="utf-8",
                ) as stream:
                    json.dump(session_cookies, stream)
                temporary.replace(settings.profile / "session-cookies.json")
        except PlaywrightError:
            LOG.warning("Браузер уже закрыт или не смог корректно сохранить профиль.")
        finally:
            # Освобождаем профиль при завершении запуска.
            try:
                context.close()
            except PlaywrightError:
                LOG.warning("Не удалось штатно закрыть браузер.")


def first_page(context: BrowserContext) -> Page:
    return next((page for page in context.pages if not page.is_closed()), None) or context.new_page()


def check_session(page: Page) -> None:
    """Проверяет доступ к личной странице, а не просто наличие cookies."""
    navigate(page, ACCOUNT_URL)

    current = urlparse(page.url)
    expected_host = urlparse(ACCOUNT_URL).hostname or "hh.ru"
    host = current.hostname or ""
    on_hh = host == expected_host or host.endswith("." + expected_host)
    if not on_hh or not current.path.startswith("/applicant/"):
        raise SessionRequired("Сайт перенаправил на вход. Нужна авторизация соискателя.")

    try:
        page.locator(SIGNED_IN_SELECTOR).first.wait_for(state="attached")
    except PlaywrightTimeout as exc:
        check_challenge(page)
        raise SessionRequired(
            "Не удалось подтвердить вход. Если вы уже вошли, проверьте "
            "SIGNED_IN_SELECTOR в main.py: вёрстка hh.ru могла измениться."
        ) from exc


def manual_login(playwright: Playwright, settings: Settings, *, context=None) -> None:
    LOG.info("Ручной вход на hh.ru в открытом окне браузера.")
    manager = nullcontext(context) if context is not None else open_browser(playwright, settings, headless=False)
    with manager as context:
        page = first_page(context)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        while True:
            print(
                "\nВойдите в профиль соискателя в открытом браузере.\n"
                "После входа вернитесь в этот терминал. Окно браузера оставьте открытым.",
                flush=True,
            )
            try:
                input("Нажмите Enter, когда вход завершён (Ctrl+C — отмена): ")
            except EOFError as exc:
                raise RuntimeError("Для ручного входа запустите программу в терминале.") from exc
            if page.is_closed():
                raise RuntimeError("Окно входа закрыто. Запустите программу повторно.")
            try:
                check_session(page)
            except (SessionRequired, HumanRequired) as exc:
                LOG.warning("%s Завершите вход или проверку в открытом окне.", exc)
                continue
            LOG.info("Вход подтверждён.")
            return


class BrowserSession:
    """Один браузер; основная страница сохраняется, включая ожидание CAPTCHA."""

    def __init__(self, playwright, settings: Settings, control: RunControl):
        self.playwright, self.settings, self.control = playwright, settings, control
        self._manager = None
        self.context = self.page = None
        self.relay = None

    def __enter__(self):
        self.open(headless=False)
        return self

    def __exit__(self, *_):
        self.close()

    def open(self, *, headless):
        manager = open_browser(self.playwright, self.settings, headless=headless)
        self.context = manager.__enter__()
        self._manager = manager
        self.page = first_page(self.context)

    def close(self):
        manager, self._manager = self._manager, None
        if manager:
            manager.__exit__(None, None, None)

    def recover(self, challenge: HumanRequired):
        if challenge.kind != "captcha" or self.relay is None:
            raise challenge
        self.relay.solve(self.page, challenge)
        return self.page


def run_background(playwright: Playwright, settings: Settings, *, login=False) -> None:
    LOG.info("Запускаю видимый браузер. Окно можно свернуть; оно остаётся доступным во время работы.")
    control = RunControl()
    try:
        client = None
        if settings.console_control and settings.config_path.exists():
            config = json.loads(settings.config_path.read_text(encoding="utf-8-sig"))
            if config.get("telegram", {}).get("enabled", False):
                client = TelegramCaptcha(config["telegram"], settings.config_path.parent)
        relay = CaptchaRelay(client, control) if client else None
        with BrowserSession(playwright, settings, control) as session:
            session.relay = relay
            if login:
                manual_login(playwright, settings, context=session.context)
            # Enter для входа читает manual_login; команды подключаем после него.
            if settings.console_control:
                control.start_console()
            with captcha_handler(relay.solve if relay else None):
                while True:
                    try:
                        check_session(session.page)
                        break
                    except HumanRequired as challenge:
                        session.recover(challenge)
                if settings.continuous:
                    run_continuous(session.page, task=settings.task, config_path=settings.config_path,
                                   dry_run=settings.dry_run, control=control, on_human_required=session.recover,
                                   page_provider=lambda: session.page)
                else:
                    run_actions(session.page, task=settings.task, config_path=settings.config_path,
                                dry_run=settings.dry_run, control=control, on_human_required=session.recover)
    finally:
        control.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", default="start", choices=("start", "login", "run"),
        help="start: ручной вход и работа в том же окне; login: только вход; run: работа с сохранённым входом",
    )
    parser.add_argument(
        "--browser", choices=("msedge", "chrome", "chromium"),
        default="msedge" if os.name == "nt" else "chromium",
        help="браузер; в Windows по умолчанию установленный Microsoft Edge",
    )
    parser.add_argument("--profile", type=Path, help="отдельная папка профиля программы")
    parser.add_argument("--timeout", type=int, default=30, help="тайм-аут операций в секундах")
    parser.add_argument("--task", choices=("bump", "search", "apply", "all"), default="all",
                        help="bump: поднять все резюме; search: собрать вакансии; apply: отклики; all: поднятие и отклики")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true", help="пробный запуск без поднятия, избранного и отправки")
    parser.add_argument("--once", action="store_true", help="один проход вместо постоянной работы")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout должен быть больше нуля")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_console_logging()
    settings = Settings(
        profile=(args.profile or default_profile(args.browser)).expanduser().resolve(),
        browser=args.browser,
        timeout_ms=args.timeout * 1000,
        task=args.task, config_path=args.config.resolve(), dry_run=args.dry_run,
        continuous=not args.once, console_control=True,
    )
    try:
        if args.command in {"start", "run"} and args.task != "bump":
            config = load_config(settings.config_path)
            if args.task in {"apply", "all"} or config["search_mode"] == "model":
                if "openai" not in config:
                    raise RuntimeError("Укажите блок openai в config.json: текущая версия использует OpenAI API.")
                validate_config(config["openai"])
                load_api_key(config["openai"], prompt=True)
        with sync_playwright() as playwright:
            if args.command == "login":
                manual_login(playwright, settings)
            if args.command in {"start", "run"}:
                run_background(playwright, settings, login=args.command == "start")
    except SessionRequired as exc:
        LOG.error("%s Выполните команду login с теми же --browser и --profile.", exc)
        return 2
    except HumanRequired as exc:
        LOG.error("%s Запустите login вручную; при ограничении частоты повторите позже.", exc)
        return 3
    except PlaywrightTimeout:
        LOG.error("Превышено время ожидания браузера или страницы. Проверьте сеть и --timeout.")
        return 1
    except PlaywrightError as exc:
        LOG.error("Ошибка браузера: %s", exc)
        LOG.error(
            "Проверьте, что профиль не занят другим запуском. Для Chromium: "
            "python -m playwright install chromium; затем --browser chromium."
        )
        return 1
    except (OSError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.info("Остановлено пользователем.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
