"""Человек читает CAPTCHA в Telegram; ответ вводится в исходную открытую форму."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError

from hh_ui import captcha_present
from response_guard import ACTIVE_RESPONSE_GUARD
from telegram_captcha import TelegramError, TelegramWork
from runtime_control import CycleExpired, RunStopped


LOG = logging.getLogger("hh_browser")
INPUTS = 'input[name="captchaText"], input[name="captcha"], input[data-qa="captcha-input"], input[data-qa="captcha-text"]'
IMAGES = 'img[data-qa*="captcha"], img[src*="captcha"], [data-qa="captcha-image"], .bloko-captcha__image, .captcha__image, canvas[data-qa*="captcha"]'
LETTER = 'textarea[data-qa="vacancy-response-popup-form-letter-input"], textarea[name="letter"]'
REFRESH = '[data-qa="captcha-refresh"], [data-qa="captcha-reload"], .bloko-captcha__refresh, .bloko-captcha__reload'


class CaptchaImageUnavailable(RuntimeError):
    pass


def image_state(image):
    return image.evaluate("""element => {
        const images = element.tagName === 'IMG' ? [element] : [...element.querySelectorAll('img')].filter(img => {
            const rect = img.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && getComputedStyle(img).visibility !== 'hidden';
        });
        if (images.length) {
            const missing = images.find(img => !img.complete || img.naturalWidth === 0 || img.naturalHeight === 0);
            const target = missing || images[0];
            const timing = performance.getEntriesByName(target.currentSrc || target.src).slice(-1)[0];
            return {ready: !missing, tag: element.tagName,
                    status: timing?.responseStatus || 0};
        }
        const drawing = element.matches('svg,canvas') ? element : element.querySelector('svg,canvas');
        return {ready: !!drawing, tag: element.tagName, status: 0};
    }""")


@dataclass
class CaptchaView:
    input: object
    image: object
    form: object

    @classmethod
    def find(cls, page):
        for frame in page.frames:
            for field in frame.locator(INPUTS).all():
                if not field.is_visible() or not field.is_enabled():
                    continue
                form = field.locator("xpath=ancestor::form[1]")
                scope = form if form.count() else frame
                candidates = []
                for image in scope.locator(IMAGES).all():
                    if image.is_visible():
                        state = image_state(image)
                        candidates.append((image, state))
                if candidates:
                    candidates.sort(key=lambda item: (not item[1]["ready"], item[1]["tag"] != "IMG"))
                    return cls(field, candidates[0][0], form if form.count() else None)
        raise RuntimeError("Не найдена текстовая CAPTCHA с картинкой. Страница оставлена открытой.")

    def capture(self, *, timeout_ms=10000, repair=True, check_stopped=None):
        page = self.input.page
        state = {"status": 0}
        for attempt in range(2 if repair else 1):
            self.image.scroll_into_view_if_needed()
            until = time.monotonic() + timeout_ms / 1000
            while True:
                if check_stopped:
                    check_stopped()
                state = image_state(self.image)
                if state["ready"]:
                    decoded = self.image.evaluate("""async element => {
                        const images = element.tagName === 'IMG' ? [element] : [...element.querySelectorAll('img')];
                        return await Promise.race([
                            Promise.all(images.map(img => img.decode())).then(() => true, () => false),
                            new Promise(resolve => setTimeout(() => resolve(false), 2000))
                        ]);
                    }""")
                    if decoded:
                        picture = self.image.screenshot(type="png")
                        # Не отправляем снимок, если источник успел сломаться во время захвата.
                        if image_state(self.image)["ready"]:
                            return picture, hashlib.sha256(picture).hexdigest()
                if time.monotonic() >= until:
                    break
                page.wait_for_timeout(100)
            if attempt == 0 and repair and self.retry_image():
                continue
            break
        status = f' (HTTP {state["status"]})' if state.get("status") else ""
        raise CaptchaImageUnavailable("Изображение CAPTCHA не загрузилось" + status + ". Пустой снимок не отправлен.")

    def retry_image(self):
        scope = self.form if self.form is not None else self.image.locator("xpath=..")
        refreshes = scope.locator(REFRESH).all()
        name = re.compile(r"^(?:Обновить (?:код|картинку|капчу)|Другая картинка|Показать другую картинку|Сменить (?:картинку|код))$", re.I)
        refreshes.extend(scope.get_by_role("button", name=name).all())
        refreshes.extend(scope.get_by_role("link", name=name).all())
        for refresh in refreshes:
            if not refresh.is_visible() or not refresh.is_enabled():
                continue
            safe = refresh.evaluate("""element => {
                if (element.tagName === 'BUTTON' && element.type === 'submit') return false;
                if (element.tagName === 'A') {
                    const href = element.getAttribute('href') || '';
                    return !href || href === '#' || href.startsWith('javascript:');
                }
                return true;
            }""")
            if safe:
                refresh.click()
                return True
        # Повторяем загрузку только исходного ресурса, сохраняя его URL/ключ.
        # Страница, форма, cookies и браузер не перезагружаются.
        return self.image.evaluate("""element => {
            const image = element.tagName === 'IMG' ? element : element.querySelector('img');
            if (!image) return false;
            const source = image.currentSrc || image.src;
            if (!/^https?:/.test(source)) return false;
            image.loading = 'eager';
            image.removeAttribute('src');
            image.setAttribute('src', source);
            return true;
        }""")

    def submit(self, answer):
        guard = ACTIVE_RESPONSE_GUARD.get()
        response_form = self.form is not None and self.form.get_attribute("name") == "vacancy_response"
        if response_form and guard is None:
            raise RuntimeError("Нельзя отправить форму отклика без проверки резюме и сопроводительного письма.")
        if response_form and guard.dry_run:
            raise RuntimeError("Пробный режим: подтверждение CAPTCHA отправило бы отклик. Отправка отключена; страница сохранена.")
        if guard and response_form:
            guard.prepare_captcha_retry()
            letter = self.form.locator(LETTER).first
            if letter.count():
                if not letter.is_visible():
                    toggle = self.form.locator('[data-qa="vacancy-response-letter-toggle"]').first
                    if toggle.count():
                        toggle.click()
                if letter.is_visible():
                    letter.fill(guard.letter)
        self.input.fill(answer)
        if self.form is not None:
            submit = self.form.locator('[data-qa="vacancy-response-submit-popup"]').first
            if not submit.count():
                submit = self.form.get_by_role("button", name=re.compile(r"^(?:Продолжить|Подтвердить|Проверить|Отправить|Войти|Готово)$", re.I)).first
            if not submit.count():
                submit = self.form.locator('button[type="submit"], input[type="submit"]').first
            if submit.count() and submit.is_visible():
                submit.click()
                return
        self.input.press("Enter")


class CaptchaRelay:
    def __init__(self, client, control, *, verification_timeout=10):
        self.client, self.control = client, control
        self.verification_timeout = verification_timeout

    def solve(self, page, challenge=None):
        guard = ACTIVE_RESPONSE_GUARD.get()
        if guard:
            previous = guard.captcha_submission, guard.captcha_checkpoint
            guard.captcha_submission = True
            guard.captcha_checkpoint = self.control.checkpoint
        try:
            return self._solve(page, challenge)
        finally:
            if guard:
                guard.captcha_submission, guard.captcha_checkpoint = previous

    def _solve(self, page, challenge=None):
        worker = TelegramWork(page, self.control)
        LOG.warning("CAPTCHA: ответьте в Telegram или пройдите проверку прямо в открытом браузере. Страница остаётся открытой.")
        unsupported_notified = False
        image_error_notified = False
        while True:
            self.control.check_stopped()
            try:
                if not captcha_present(page):
                    return
                view = CaptchaView.find(page)
                picture, fingerprint = view.capture(check_stopped=self.control.check_stopped)
                image_error_notified = False
                answer = worker.call(lambda cancelled: self.client.request_answer(picture, cancelled=cancelled),
                                     finished=lambda: not captcha_present(page))
                self.control.checkpoint()
                if answer is None or not captcha_present(page):
                    LOG.info("CAPTCHA пройдена в браузере. Продолжаю работу на той же странице.")
                    worker.call(lambda _: self.client.send_text("CAPTCHA пройдена в браузере. Ответ в Telegram больше не нужен; продолжаю работу."))
                    return  # Ответ на уже исчезнувшую проверку не вводится.
                view = CaptchaView.find(page)
                if view.capture(repair=False, check_stopped=self.control.check_stopped)[1] != fingerprint:
                    worker.call(lambda _: self.client.send_text("Картинка CAPTCHA успела измениться. Сейчас пришлю новую; прежний ответ не использован."))
                    continue
                view.submit(answer)
                until = time.monotonic() + self.verification_timeout
                while time.monotonic() < until and captcha_present(page):
                    self.control.check_stopped()
                    page.wait_for_timeout(200)
                if not captcha_present(page):
                    LOG.info("CAPTCHA пройдена. Продолжаю работу в том же браузере и на той же странице.")
                    worker.call(lambda _: self.client.send_text("CAPTCHA пройдена. Продолжаю работу в том же браузере."))
                    return
                LOG.warning("Ответ CAPTCHA не принят или появилась новая картинка; запрашиваю новый ответ в Telegram.")
                worker.call(lambda _: self.client.send_text("Ответ не принят или CAPTCHA обновилась. Пришлю следующую картинку."))
            except TelegramError as exc:
                LOG.error("%s Браузер оставлен открытым; повторю подключение к Telegram.", exc)
                self.wait(page, 5)
            except CaptchaImageUnavailable as exc:
                LOG.error("%s Страница остаётся открытой; повторю загрузку картинки.", exc)
                if not image_error_notified:
                    try:
                        worker.call(lambda _: self.client.send_text("Изображение CAPTCHA пока не загрузилось на сайте. Пустую картинку не отправляю; повторю загрузку, сохраняя браузер и форму."))
                    except TelegramError:
                        pass
                    image_error_notified = True
                self.wait(page, 30)
            except (RunStopped, CycleExpired):
                raise
            except RuntimeError as exc:
                # Неизвестный вид CAPTCHA или неопределённый результат отправки:
                # не перезагружаем страницу и не повторяем отклик автоматически.
                LOG.error("%s Ожидаю, сохраняя страницу и сессию.", exc)
                if not unsupported_notified:
                    try:
                        worker.call(lambda _: self.client.send_text(str(exc) + " Автоматические действия приостановлены; страница не закрывается."))
                    except TelegramError:
                        pass
                    unsupported_notified = True
                self.wait(page, 30)
            except PlaywrightError:
                LOG.error("Состояние CAPTCHA изменилось. Проверяю исходную страницу без перезагрузки.")
                self.wait(page, 2)

    def wait(self, page, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.control.check_stopped()
            if page.is_closed():
                raise RuntimeError("Страница закрыта пользователем.")
            page.wait_for_timeout(200)
