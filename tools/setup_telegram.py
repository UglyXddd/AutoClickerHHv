"""Привязать личный чат разрешённого пользователя и проверить Telegram."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telegram_captcha import TelegramCaptcha, TelegramError

root = Path(__file__).resolve().parents[1]
try:
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    client = TelegramCaptcha(config["telegram"], root)
    bot = client.connect()
    if client.bound:
        client.send_text("Бот подключён к проекту hh.ru. При CAPTCHA пришлю картинку; ответьте её символами одним сообщением. Браузер и страница при этом останутся открытыми.")
    print(json.dumps({"bot": bot["username"], "bound": client.bound, "allowed_username": client.username}, ensure_ascii=False))
except (TelegramError, OSError, ValueError) as exc:
    print(type(exc).__name__ + ": " + str(exc))
    raise SystemExit(1)
