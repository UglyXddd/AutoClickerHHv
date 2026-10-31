"""Проверки на локальном сайте: в hh.ru и пользовательский профиль не обращаются."""

from __future__ import annotations

import os
import base64
import json
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from unittest.mock import Mock
from urllib.parse import parse_qs

from playwright.sync_api import sync_playwright

import main as app
from runtime_control import RunControl
from captcha_relay import CaptchaRelay


class LocalSite(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/captcha-image.png":
            image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1sAAAAASUVORK5CYII=")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(image)
            return
        if self.path == "/sign-in":
            self.send_response(302)
            # Намеренно session cookie: у него нет Max-Age и Expires.
            self.send_header("Set-Cookie", "test_session=valid; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/applicant/resumes")
            self.end_headers()
            return
        if self.path == "/applicant/resumes":
            mode = self.server.mode
            if mode in {"blocked", "rate_limited", "error"}:
                self.send_response({"blocked": 403, "rate_limited": 429, "error": 500}[mode])
                self.end_headers()
                return
            if "test_session=valid" not in self.headers.get("Cookie", ""):
                self.send_response(302)
                self.send_header("Location", "/account/login")
                self.end_headers()
                return
            body = '<button data-qa="mainmenu_applicantProfile">Профиль</button>'
            if mode == "captcha":
                body += '''<form method="POST" action="/solve-captcha">
                <img data-qa="captcha-image" src="/captcha-image.png" width="180" height="60">
                <input name="captchaText" aria-label="Проверка"><button type="submit">Проверить</button></form>'''
            if mode == "changed_markup":
                body = "Страница без признака авторизации"
            self.html(body, status=403 if mode == "captcha" else 200)
            return
        self.html('<a href="/sign-in">Войти</a>')

    def do_POST(self):
        fields = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        if self.path == "/solve-captcha" and fields.get("captchaText") == ["Ab42"]:
            self.server.mode = "ok"
        self.send_response(303)
        self.send_header("Location", "/applicant/resumes")
        self.end_headers()

    def html(self, body: str, status=200) -> None:
        payload = f"<!doctype html><html><head><title>Local account</title></head><body>{body}</body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class BrowserIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalSite)
        cls.server.mode = "ok"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright_manager = sync_playwright()
        cls.playwright = cls.playwright_manager.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.server.mode = "ok"
        # Профиль проверки всегда создаётся отдельно от настоящего профиля hh.ru.
        self.temporary = TemporaryDirectory(prefix="hh-browser-test-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = app.Settings(
            profile=Path(self.temporary.name) / "profile",
            browser=os.environ.get("HH_TEST_BROWSER", "msedge" if os.name == "nt" else "chromium"),
            timeout_ms=15_000,
        )
        self.account_patch = patch.object(app, "ACCOUNT_URL", self.origin + "/applicant/resumes")
        self.account_patch.start()
        self.addCleanup(self.account_patch.stop)
        # Проверяем запрошенный режим окна, но локальные тесты не открывают GUI.
        real_open = app.open_browser

        def hidden_test_browser(playwright, settings, *, headless):
            return real_open(playwright, settings, headless=True)

        browser_patch = patch.object(app, "open_browser", hidden_test_browser)
        browser_patch.start()
        self.addCleanup(browser_patch.stop)

    def sign_in(self, page) -> None:
        page.goto(self.origin + "/sign-in")

    def test_start_keeps_login_window_page_and_session_storage(self) -> None:
        """Вход и действия используют одно видимое окно и одну вкладку."""
        open_real_browser = app.open_browser
        requested_modes = []
        control = RunControl()
        settings = replace(self.settings, console_control=True,
                           config_path=Path(self.temporary.name) / "unused-config.json")

        def no_visible_window(playwright, settings, *, headless):
            requested_modes.append(headless)
            return open_real_browser(playwright, settings, headless=True)

        def manual_input(_prompt):
            start_console.assert_not_called()
            active_page = self.active_context.pages[0]
            self.login_page = active_page
            self.sign_in(active_page)
            active_page.evaluate("localStorage.setItem('test_value', 'retained')")
            active_page.evaluate("sessionStorage.setItem('same_tab', 'retained')")
            return ""

        # Обёртка нужна лишь для доступа к странице, на которой человек нажал бы «Войти».
        from contextlib import contextmanager

        @contextmanager
        def browser_for_login(playwright, settings, *, headless):
            with no_visible_window(playwright, settings, headless=headless) as context:
                self.active_context = context
                yield context

        result = []

        def action(page, **kwargs):
            self.assertIs(page, self.login_page)
            start_console.assert_called_once_with()
            result.append((page.title(), page.evaluate("localStorage.getItem('test_value')"),
                           page.evaluate("sessionStorage.getItem('same_tab')")))

        with (
            patch.object(app, "LOGIN_URL", self.origin + "/account/login"),
            patch.object(app, "open_browser", browser_for_login),
            patch("builtins.input", side_effect=manual_input),
            patch("builtins.print"),
            patch.object(app, "run_actions", side_effect=action),
            patch.object(app, "RunControl", return_value=control),
            patch.object(control, "start_console") as start_console,
        ):
            app.run_background(self.playwright, settings, login=True)

        self.assertEqual(requested_modes, [False])
        self.assertEqual(result, [("Local account", "retained", "retained")])

    def test_expired_session_never_starts_actions(self) -> None:
        with patch.object(app, "run_actions") as action:
            with self.assertRaises(app.SessionRequired):
                app.run_background(self.playwright, self.settings)
            action.assert_not_called()

    def test_telegram_captcha_keeps_browser_page_and_session_storage(self) -> None:
        from contextlib import contextmanager

        with app.open_browser(self.playwright, self.settings, headless=True) as context:
            page = app.first_page(context)
            self.sign_in(page)
            page.evaluate("localStorage.setItem('test_value','retained')")
        self.server.mode = "captcha"
        settings = replace(self.settings, console_control=True)
        config_path = Path(self.temporary.name) / "config.json"
        config_path.write_text(json.dumps({"telegram": {"enabled": True}}), encoding="utf-8")
        settings = replace(settings, config_path=config_path)
        control = RunControl()
        original_open = app.open_browser
        requested_modes, actions, pages = [], [], []
        client = Mock()
        client.request_answer.side_effect = ["wrong", "Ab42"]

        @contextmanager
        def no_visible_window(playwright, settings, *, headless):
            requested_modes.append(headless)
            with original_open(playwright, settings, headless=True) as context:
                self.active_context = context
                page = app.first_page(context)
                self.sign_in(page)
                page.evaluate("sessionStorage.setItem('captcha_tab','same')")
                pages.append(page)
                yield context

        def action(page, **kwargs):
            self.assertIs(page, pages[0])
            actions.append((page.evaluate("localStorage.getItem('test_value')"), page.evaluate("sessionStorage.getItem('captcha_tab')")))

        with (patch.object(app, "open_browser", no_visible_window),
              patch.object(app, "RunControl", return_value=control),
              patch.object(control, "start_console"),
              patch.object(app, "TelegramCaptcha", return_value=client),
              patch.object(app, "CaptchaRelay", side_effect=lambda api, ctl: CaptchaRelay(api, ctl, verification_timeout=0.1)),
              patch.object(app, "run_actions", side_effect=action)):
            app.run_background(self.playwright, settings)
        self.assertEqual(requested_modes, [False])
        self.assertEqual(client.request_answer.call_count, 2)
        self.assertEqual(actions, [("retained", "same")])

    def test_captcha_recovery_does_not_close_open_or_navigate_browser(self):
        session = app.BrowserSession(None, self.settings, RunControl())
        session.page = Mock()
        session.relay = Mock()
        challenge = app.HumanRequired("CAPTCHA", url=self.origin + "/applicant/vacancy_response?vacancyId=1")
        with patch.object(session, "open") as opened, patch.object(session, "close") as closed:
            self.assertIs(session.recover(challenge), session.page)
            opened.assert_not_called()
            closed.assert_not_called()
            session.page.goto.assert_not_called()
            session.page.reload.assert_not_called()
            session.relay.solve.assert_called_once_with(session.page, challenge)

    def test_blocked_and_captcha_pages_never_start_actions(self) -> None:
        for mode in ("blocked", "rate_limited", "captcha"):
            with self.subTest(mode=mode):
                with app.open_browser(self.playwright, self.settings, headless=True) as context:
                    self.sign_in(app.first_page(context))
                self.server.mode = mode
                with patch.object(app, "run_actions") as action:
                    with self.assertRaises(app.HumanRequired):
                        app.run_background(self.playwright, self.settings)
                    action.assert_not_called()
                self.server.mode = "ok"

    def test_changed_markup_and_server_error_do_not_count_as_login(self) -> None:
        with app.open_browser(self.playwright, self.settings, headless=True) as context:
            page = app.first_page(context)
            self.sign_in(page)
            page.set_default_timeout(200)
            self.server.mode = "changed_markup"
            with self.assertRaises(app.SessionRequired):
                app.check_session(page)
            self.server.mode = "error"
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                app.check_session(page)

    def test_browser_closes_on_action_failure_and_profile_can_be_reused(self) -> None:
        with app.open_browser(self.playwright, self.settings, headless=True) as context:
            self.sign_in(app.first_page(context))
        with patch.object(app, "run_actions", side_effect=RuntimeError("failed action")):
            with self.assertRaisesRegex(RuntimeError, "failed action"):
                app.run_background(self.playwright, self.settings)
        # Этот запуск не удался бы, если бы предыдущий браузер удерживал профиль.
        with app.open_browser(self.playwright, self.settings, headless=True) as context:
            app.check_session(app.first_page(context))


if __name__ == "__main__":
    unittest.main()
