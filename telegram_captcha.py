"""Доставка изображения человеку и приём ответа через официальный Telegram Bot API."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from http.client import HTTPException
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LOG = logging.getLogger("hh_browser")


class TelegramError(RuntimeError):
    pass


class TelegramCaptcha:
    def __init__(self, config: dict, root: Path):
        self.root = root
        self.token_path = root / config.get("token_file", ".telegram-bot-token")
        self.state_path = root / config.get("state_file", ".telegram-state.json")
        self.username = config.get("allowed_username", "").lstrip("@").casefold()
        if not self.username:
            raise TelegramError("Не задан telegram.allowed_username.")
        self.token = self.token_path.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", self.token):
            raise TelegramError("Некорректный токен Telegram в локальном файле.")
        self.bot_id = int(self.token.split(":", 1)[0])
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.state = {}
        except ValueError:
            raise TelegramError("Не читается локальное состояние Telegram; проверьте файл привязки.") from None
        if self.state.get("bot_id") != self.bot_id or self.state.get("allowed_username") != self.username:
            self.state = {"bot_id": self.bot_id, "allowed_username": self.username, "offset": 0}

    @property
    def bound(self):
        return bool(self.state.get("chat_id") and self.state.get("user_id"))

    def save(self):
        temporary = self.state_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_path)
        except OSError:
            raise TelegramError("Не удалось сохранить локальное состояние Telegram.") from None

    def api(self, method: str, params: dict | None = None, *, photo: bytes | None = None):
        params = params or {}
        if photo is None:
            data = json.dumps(params).encode("utf-8")
            content_type = "application/json"
        else:
            boundary = "HHcaptcha" + secrets.token_hex(16)
            chunks = []
            for key, value in params.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value)
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode("utf-8"))
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="captcha.png"\r\nContent-Type: image/png\r\n\r\n').encode())
            chunks.extend((photo, (f"\r\n--{boundary}--\r\n").encode()))
            data, content_type = b"".join(chunks), "multipart/form-data; boundary=" + boundary
        request = Request("https://api.telegram.org/bot" + self.token + "/" + method, data=data,
                          headers={"Content-Type": content_type}, method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise TelegramError(f"Telegram Bot API: HTTP {exc.code}. Метод {method}.") from None
        except (URLError, TimeoutError, OSError, ValueError, HTTPException):
            # Текст низкоуровневого исключения способен содержать URL с токеном.
            raise TelegramError(f"Нет корректного ответа Telegram. Метод {method}.") from None
        if not isinstance(result, dict) or not result.get("ok"):
            raise TelegramError(f"Telegram не выполнил {method}; код {result.get('error_code', '?') if isinstance(result, dict) else '?' }.")
        return result["result"]

    def receive(self, *, timeout=0):
        updates = self.api("getUpdates", {"offset": self.state.get("offset", 0), "limit": 100,
                                          "timeout": timeout, "allowed_updates": ["message"]})
        messages = []
        for update in updates:
            self.state["offset"] = max(self.state.get("offset", 0), update["update_id"] + 1)
            message = update.get("message", {})
            sender, chat = message.get("from", {}), message.get("chat", {})
            if chat.get("type") != "private" or sender.get("is_bot") or chat.get("id") != sender.get("id"):
                continue
            if self.bound:
                allowed = sender.get("id") == self.state["user_id"] and chat.get("id") == self.state["chat_id"]
            else:
                allowed = sender.get("username", "").casefold() == self.username
                if allowed:
                    self.state.update(chat_id=chat["id"], user_id=sender["id"])
            if allowed:
                messages.append(message)
        if updates:
            self.save()
        return messages

    def connect(self):
        bot = self.api("getMe")
        if self.api("getWebhookInfo").get("url"):
            raise TelegramError("У бота установлен webhook. Для этого проекта нужен отдельный бот без webhook.")
        self.receive()
        return bot

    def send_text(self, text):
        if self.bound:
            return self.api("sendMessage", {"chat_id": self.state["chat_id"], "text": text})

    def request_answer(self, photo: bytes, *, cancelled, caption=None):
        # Все HTTP-вызовы выполняются в отдельном потоке; этот метод не трогает браузер.
        while not self.bound:
            if cancelled():
                raise TelegramError("Ожидание Telegram остановлено.")
            self.receive(timeout=5)
        while not cancelled() and self.receive():
            pass  # Старые сообщения не могут стать ответом на новую картинку.
        if cancelled():
            raise TelegramError("Ожидание Telegram остановлено.")
        prompt = self.api("sendPhoto", {
            "chat_id": self.state["chat_id"],
            "caption": caption or "hh.ru запросил CAPTCHA. Пришлите символы с картинки одним сообщением. Регистр сохраняется. Страница в браузере остаётся открытой.",
            "reply_markup": {"force_reply": True, "selective": True},
        }, photo=photo)
        while not cancelled():
            for message in self.receive(timeout=5):
                if message.get("message_id", 0) <= prompt["message_id"]:
                    continue
                reply = message.get("reply_to_message")
                if reply and reply.get("message_id") != prompt["message_id"]:
                    continue
                text = message.get("text", "").strip()
                if not text or text.startswith("/") or len(text) > 128 or "\n" in text or "\r" in text:
                    continue
                return text
        raise TelegramError("Ожидание Telegram остановлено.")


class TelegramWork:
    """Ожидание HTTP без блокирования событий Playwright и команд консоли."""

    def __init__(self, page, control):
        self.page, self.control = page, control

    def call(self, operation, *, finished=None):
        import threading
        stop = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-captcha")
        future = executor.submit(operation, stop.is_set)
        completed = False
        try:
            while not future.done():
                if self.page.is_closed():
                    raise RuntimeError("Страница CAPTCHA закрыта пользователем.")
                self.control.check_stopped()
                if not completed and finished and finished():
                    completed = True
                    stop.set()
                self.page.wait_for_timeout(200)
            # Дожидаемся завершения отменённого HTTP-опроса, чтобы следующий
            # запрос CAPTCHA не начал читать Telegram одновременно с прежним.
            if completed or (finished and finished()):
                return None
            return future.result()
        finally:
            stop.set()
            executor.shutdown(wait=False, cancel_futures=True)
