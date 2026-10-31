import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import URLError

from telegram_captcha import TelegramCaptcha, TelegramError


class TelegramTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="hh-telegram-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".telegram-bot-token").write_text("12345:fake_token", encoding="utf-8")
        self.client = TelegramCaptcha({"allowed_username": "ugly_xddd"}, self.root)

    def update(self, update_id, *, user_id=42, username="ugly_xddd", chat_id=None, text="/start", message_id=1, reply=None):
        message = {"from": {"id": user_id, "username": username, "is_bot": False},
                   "chat": {"id": user_id if chat_id is None else chat_id, "type": "private"},
                   "text": text, "message_id": message_id}
        if reply:
            message["reply_to_message"] = {"message_id": reply}
        return {"update_id": update_id, "message": message}

    def test_binding_pins_numeric_sender_and_private_chat(self):
        with patch.object(self.client, "api", return_value=[self.update(1, user_id=100, username="stranger"),
                                                          self.update(2, chat_id=-100), self.update(3)]):
            self.assertEqual(len(self.client.receive()), 1)
        self.assertTrue(self.client.bound)
        with patch.object(self.client, "api", return_value=[self.update(4, user_id=999),
                                                          self.update(5, username="new_name", text="Ab42")]):
            accepted = self.client.receive()
        self.assertEqual([item["text"] for item in accepted], ["Ab42"])
        self.assertEqual(self.client.state["offset"], 6)
        self.assertNotIn("fake_token", self.client.state_path.read_text())

    def test_only_fresh_single_message_for_current_picture_is_accepted(self):
        self.client.state.update(chat_id=42, user_id=42)
        batches = [[], [self.update(1, text="old", message_id=99), self.update(2, text="/start", message_id=101),
                         self.update(3, text="wrong picture", message_id=102, reply=90),
                         self.update(4, text="from stranger", user_id=99, message_id=103)],
                   [self.update(5, text=" Ab42 ", message_id=104, reply=100)]]

        def api(method, params, **kwargs):
            if method == "getUpdates":
                return batches.pop(0)
            self.assertEqual(method, "sendPhoto")
            self.assertEqual(kwargs["photo"], b"png-image")
            return {"message_id": 100}

        with patch.object(self.client, "api", side_effect=api):
            self.assertEqual(self.client.request_answer(b"png-image", cancelled=lambda: False), "Ab42")

    def test_transport_errors_never_expose_token(self):
        with patch("telegram_captcha.urlopen", side_effect=URLError("https://api.telegram.org/bot12345:fake_token/getMe")):
            with self.assertRaises(TelegramError) as failure:
                self.client.api("getMe")
        self.assertNotIn("fake_token", str(failure.exception))

    def test_photo_uses_multipart_upload(self):
        with patch("telegram_captcha.urlopen", return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')) as request:
            result = self.client.api("sendPhoto", {"chat_id": 42, "caption": "CAPTCHA"}, photo=b"PNG")
        self.assertEqual(result["message_id"], 1)
        payload = request.call_args.args[0]
        self.assertIn("multipart/form-data", payload.headers["Content-type"])
        self.assertIn(b'name="photo"; filename="captcha.png"', payload.data)
        self.assertIn(b"PNG", payload.data)

    def test_existing_webhook_is_not_deleted(self):
        with patch.object(self.client, "api", side_effect=[{"username": "test_bot"}, {"url": "https://existing.example/hook"}]) as api:
            with self.assertRaises(TelegramError):
                self.client.connect()
        self.assertEqual([call.args[0] for call in api.call_args_list], ["getMe", "getWebhookInfo"])
