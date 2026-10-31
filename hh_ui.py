"""Общие проверки страниц и переходы без обхода ограничений сайта."""

from urllib.parse import urlparse
from contextlib import contextmanager
from contextvars import ContextVar

from playwright.sync_api import Error as PlaywrightError, Page


class SessionRequired(RuntimeError):
    pass


class HumanRequired(RuntimeError):
    def __init__(self, message, *, url="", kind="captcha", status=None):
        super().__init__(message)
        self.url, self.kind, self.status = url, kind, status


CHALLENGE_SELECTOR = ", ".join((
    'iframe[src*="captcha"]', '[data-qa="captcha"]', 'input[name="captcha"]',
    'input[name="captchaText"]', '.g-recaptcha', '.smart-captcha',
))
_CAPTCHA_HANDLER = ContextVar("captcha_handler", default=None)


@contextmanager
def captcha_handler(handler):
    token = _CAPTCHA_HANDLER.set(handler)
    try:
        yield
    finally:
        _CAPTCHA_HANDLER.reset(token)


def captcha_present(page: Page) -> bool:
    path = urlparse(page.url).path.lower()
    return "captcha" in path or "challenge" in path or any(
        item.is_visible() for item in page.locator(CHALLENGE_SELECTOR).all()
    )


def check_challenge(page: Page, status: int | None = None) -> bool:
    if captcha_present(page):
        challenge = HumanRequired("На странице появилась CAPTCHA. Нужна ручная проверка.", url=page.url, status=status)
        handler = _CAPTCHA_HANDLER.get()
        if handler:
            handler(page, challenge)
            return True
        raise challenge
    if status in {403, 429}:
        raise HumanRequired(f"Сайт ограничил доступ: HTTP {status}.", url=page.url,
                            kind="rate_limit" if status == 429 else "access_denied", status=status)
    return False


def navigate(page: Page, url: str) -> None:
    statuses: list[int] = []

    def on_response(response):
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            statuses.append(response.status)

    page.on("response", on_response)
    resolved = False
    response = None
    try:
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError:
            # Edge может прервать навигацию при пустом теле ответа 403/429.
            if statuses and statuses[-1] in {403, 429}:
                resolved = check_challenge(page, statuses[-1])
            if not resolved and statuses and statuses[-1] >= 400:
                raise RuntimeError(f"Страница недоступна: HTTP {statuses[-1]}.") from None
            if not resolved:
                raise
    finally:
        page.remove_listener("response", on_response)
    status = response.status if response and not resolved else None
    if check_challenge(page, status):
        status = None
    if status is not None and status >= 400:
        raise RuntimeError(f"Страница недоступна: HTTP {status}.")
    if "/account/login" in urlparse(page.url).path and "/account/login" not in urlparse(url).path:
        raise SessionRequired("Сессия истекла. Сайт перенаправил на вход.")
