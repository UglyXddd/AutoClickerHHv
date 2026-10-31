"""Живая проверка Telegram на локальной учебной картинке; hh.ru не открывается."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from captcha_relay import CaptchaView
from runtime_control import RunControl
from telegram_captcha import TelegramCaptcha, TelegramError, TelegramWork
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
config = json.loads((root / "config.json").read_text(encoding="utf-8"))
client = TelegramCaptcha(config["telegram"], root)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page()
    page.set_content('''<form onsubmit="event.preventDefault();document.body.dataset.result=document.querySelector('input').value;this.remove()">
    <svg data-qa="captcha-image" width="280" height="100" xmlns="http://www.w3.org/2000/svg">
      <rect width="280" height="100" fill="#f2f2f2"/><text x="50" y="66" font-size="44" font-family="monospace" fill="#14213d">Ab42</text>
    </svg><input name="captchaText"><button type="submit">Проверить</button></form>''')
    view = CaptchaView.find(page)
    picture, _ = view.capture()
    print("Тестовая картинка отправляется в ваш Telegram; ожидается один ответ. hh.ru не используется.", flush=True)
    deadline = time.monotonic() + 240
    try:
        answer = TelegramWork(page, RunControl()).call(lambda cancelled: client.request_answer(
            picture, cancelled=lambda: cancelled() or time.monotonic() > deadline,
            caption="Проверка интеграции. Это учебная картинка, не CAPTCHA с hh.ru. Пришлите символы с неё одним сообщением; проверю ввод в исходную локальную форму."))
        view.submit(answer)
        page.wait_for_timeout(100)
        passed = page.evaluate("document.body.dataset.result === 'Ab42'")
        client.send_text("Тест пройден: сообщение получено и введено в исходную форму без перезапуска браузера." if passed else "Ответ получен и введён в форму. Проверка регистра/символов тестовой картинки не совпала.")
        print(json.dumps({"answered": True, "inserted_correctly": passed, "browser_restarts": 0}), flush=True)
    except TelegramError as exc:
        print(json.dumps({"answered": False, "message": str(exc)}, ensure_ascii=False), flush=True)
    finally:
        browser.close()
