import os
import base64
import threading
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright
from captcha_relay import CaptchaImageUnavailable, CaptchaRelay, CaptchaView, image_state
from runtime_control import RunControl, RunStopped
from response_guard import ResponseGuard
from telegram_captcha import TelegramError


FORM = '''<form onsubmit="event.preventDefault();document.body.dataset.answer=document.querySelector('input').value;this.remove()">
<svg data-qa="captcha-image" width="180" height="60"><rect width="180" height="60" fill="white"/>
<text id="picture" x="10" y="40" font-size="30">Ab42</text></svg>
<input name="captchaText"><button type="submit">Проверить</button></form>'''
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMwSpkGAAH4AS3k7SvZAAAAAElFTkSuQmCC")


class CaptchaRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge" if os.name == "nt" else "chromium", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.route("http://hh.local/test", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FORM))
        self.page.goto("http://hh.local/test")
        self.page.evaluate("sessionStorage.setItem('same_tab','kept')")
        self.client = Mock()
        self.control = RunControl()
        self.relay = CaptchaRelay(self.client, self.control, verification_timeout=0.1)

    def test_code_is_inserted_in_same_page_without_navigation(self):
        self.client.request_answer.return_value = "Ab42"
        original_url = self.page.url
        self.relay.solve(self.page)
        self.assertEqual(self.page.url, original_url)
        self.assertEqual(self.page.evaluate("document.body.dataset.answer"), "Ab42")
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('same_tab')"), "kept")
        self.assertTrue(self.client.request_answer.call_args.args[0].startswith(b"\x89PNG"))

    def test_network_retry_keeps_original_form(self):
        self.client.request_answer.side_effect = [TelegramError("Нет связи"), "Ab42"]
        with patch.object(self.relay, "wait", side_effect=lambda page, _: page.wait_for_timeout(1)):
            self.relay.solve(self.page)
        self.assertEqual(self.client.request_answer.call_count, 2)
        self.assertEqual(self.page.evaluate("document.body.dataset.answer"), "Ab42")

    def test_manual_browser_solution_cancels_telegram_wait_without_submitting_again(self):
        waiting, stopped = threading.Event(), threading.Event()

        def wait_for_answer(_picture, *, cancelled):
            waiting.set()
            while not cancelled():
                stopped.wait(0.01)
            stopped.set()
            raise TelegramError("Ожидание Telegram остановлено.")

        self.client.request_answer.side_effect = wait_for_answer
        original_check = self.control.check_stopped
        submitted_manually = False

        def check():
            nonlocal submitted_manually
            original_check()
            if waiting.is_set() and not submitted_manually:
                submitted_manually = True
                self.page.locator('input[name="captchaText"]').fill("Ab42")
                self.page.get_by_role("button", name="Проверить").click()

        with (patch.object(self.control, "check_stopped", side_effect=check),
              patch.object(CaptchaView, "submit") as submit):
            self.relay.solve(self.page)
        self.assertTrue(stopped.is_set())
        submit.assert_not_called()
        self.assertEqual(self.page.evaluate("document.body.dataset.answer"), "Ab42")
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('same_tab')"), "kept")
        self.assertTrue(any("больше не нужен" in call.args[0] for call in self.client.send_text.call_args_list))

    def raster_form(self, images, extra=""):
        self.page.set_content('<form><input type="hidden" name="captchaKey" value="original-key">'
                              + images + '<input name="captchaText">' + extra + '</form>')

    def test_delayed_image_is_loaded_before_screenshot(self):
        self.page.route("**/late-captcha.png", lambda route: route.fulfill(content_type="image/png", body=PNG))
        self.raster_form('<img data-qa="captcha-image" width="180" height="60" alt="captcha">',
                         '<script>setTimeout(()=>document.querySelector("img").src="/late-captcha.png",250)</script>')
        view = CaptchaView.find(self.page)
        picture, _ = view.capture(timeout_ms=1500, repair=False)
        self.assertTrue(picture.startswith(b"\x89PNG"))
        self.assertTrue(image_state(view.image)["ready"])

    def test_broken_image_is_never_screenshotted_or_sent(self):
        self.page.route("**/broken-captcha.png", lambda route: route.fulfill(status=404, body="not an image"))
        self.raster_form('<img data-qa="captcha-image" src="/broken-captcha.png" width="180" height="60" alt="captcha">')
        view = CaptchaView.find(self.page)
        self.assertTrue(view.image.evaluate("img=>img.complete"))
        self.assertEqual(view.image.evaluate("img=>img.naturalWidth"), 0)
        with patch.object(view.image, "screenshot", wraps=view.image.screenshot) as screenshot:
            with self.assertRaises(CaptchaImageUnavailable):
                view.capture(timeout_ms=100, repair=False)
            screenshot.assert_not_called()
        original_capture = CaptchaView.capture
        with (patch.object(CaptchaView, "capture", autospec=True,
                           side_effect=lambda current, **kw: original_capture(current, timeout_ms=50, repair=False)),
              patch.object(self.relay, "wait", side_effect=RunStopped("end local test"))):
            with self.assertRaises(RunStopped):
                self.relay.solve(self.page)
        self.client.request_answer.assert_not_called()
        self.assertFalse(self.page.is_closed())
        self.assertEqual(self.page.url, "http://hh.local/test")

    def test_retry_reloads_only_same_image_not_page_or_form(self):
        requests = []

        def image(route):
            requests.append(route.request.url)
            if len(requests) == 1:
                route.abort()
            else:
                route.fulfill(content_type="image/png", body=PNG)

        self.page.route("**/retry-captcha.png*", image)
        self.raster_form('<img data-qa="captcha-image" src="/retry-captcha.png?key=original-key" width="180" height="60" alt="captcha">')
        navigations = []
        self.page.on("framenavigated", lambda frame: navigations.append(frame.url))
        view = CaptchaView.find(self.page)
        picture, _ = view.capture(timeout_ms=500)
        self.assertTrue(picture.startswith(b"\x89PNG"))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(navigations, [])
        self.assertEqual(self.page.locator('[name="captchaKey"]').input_value(), "original-key")
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('same_tab')"), "kept")

    def test_site_refresh_updates_image_and_key_without_page_navigation(self):
        self.page.route("**/bad-captcha.png", lambda route: route.fulfill(status=404, body="bad"))
        self.page.route("**/good-captcha.png", lambda route: route.fulfill(content_type="image/png", body=PNG))
        self.raster_form('<img data-qa="captcha-image" src="/bad-captcha.png" width="180" height="60">',
                         '''<button type="button" data-qa="captcha-refresh" onclick="document.querySelector('img').src='/good-captcha.png';document.querySelector('[name=captchaKey]').value='new-key'">Обновить картинку</button>''')
        view = CaptchaView.find(self.page)
        view.capture(timeout_ms=500)
        self.assertTrue(image_state(view.image)["ready"])
        self.assertEqual(self.page.locator('[name="captchaKey"]').input_value(), "new-key")
        self.assertEqual(self.page.url, "http://hh.local/test")

    def test_loaded_image_is_preferred_to_visible_broken_placeholder(self):
        self.page.route("**/bad-captcha.png", lambda route: route.fulfill(status=404, body="bad"))
        self.page.route("**/good-captcha.png", lambda route: route.fulfill(content_type="image/png", body=PNG))
        self.raster_form('<img id="bad" data-qa="captcha-image" src="/bad-captcha.png" width="180" height="60">'
                         '<img id="good" data-qa="captcha-image" src="/good-captcha.png" width="180" height="60">')
        view = CaptchaView.find(self.page)
        self.assertEqual(view.image.get_attribute("id"), "good")
        view.capture(timeout_ms=100, repair=False)

    def test_response_guard_allows_captcha_image_resource(self):
        self.page.route("**/applicant/vacancy_response/captcha-image.png", lambda route: route.fulfill(content_type="image/png", body=PNG))
        with ResponseGuard(self.page, "http://hh.local", "1", "resume", "Письмо", dry_run=False,
                           reserve=lambda: True, checkpoint=lambda: None) as guard:
            guard.active = True
            self.page.set_content('<img data-qa="captcha-image" src="/applicant/vacancy_response/captcha-image.png" width="180" height="60">')
            self.assertGreater(self.page.locator("img").evaluate("img=>img.naturalWidth"), 0)
            self.assertEqual(guard.blocked, "")

    def test_answer_to_replaced_picture_is_not_submitted(self):
        requested, changed = threading.Event(), threading.Event()

        def first_answer(*args, **kwargs):
            requested.set()
            changed.wait(timeout=3)
            return "OLD1"

        self.client.request_answer.side_effect = [first_answer, "Ab42"]
        # Mock с iterable возвращает функции как значения, поэтому вызываем
        # первый ответ явно в отдельном сетевом потоке.
        count = [0]

        def answer(*args, **kwargs):
            count[0] += 1
            return first_answer() if count[0] == 1 else "Ab42"

        self.client.request_answer.side_effect = answer
        original_check = self.control.check_stopped

        def check():
            original_check()
            if requested.is_set() and not changed.is_set():
                self.page.evaluate("document.querySelector('#picture').setAttribute('fill','red')")
                changed.set()

        with patch.object(self.control, "check_stopped", side_effect=check):
            self.relay.solve(self.page)
        self.assertEqual(count[0], 2)
        self.assertEqual(self.page.evaluate("document.body.dataset.answer"), "Ab42")
        self.assertTrue(any("измени" in call.args[0] for call in self.client.send_text.call_args_list))
