"""Responses API, локальный кэш и сохраняемый между запусками бюджет."""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


LOG = logging.getLogger("hh_browser")
ENDPOINT = "https://api.openai.com/v1/responses"
# USD за миллион токенов; официальные цены проверены 30.10.2026.
PRICES = {
    "gpt-6-luna": {"input": 0.10, "cached": 0.01, "write": 0.125, "output": 0.50},
    "gpt-5.4-mini": {"input": 0.75, "cached": 0.075, "write": 0.9375, "output": 4.50},
}


class OpenAIError(RuntimeError):
    pass


class OpenAIStop(OpenAIError):
    """Ошибка ключа, бюджета или неизвестный результат платного запроса: остановка."""


def validate_config(config):
    if config.get("model") not in PRICES:
        raise OpenAIStop("Для учёта расходов выберите openai.model: gpt-6-luna или gpt-5.4-mini.")
    for name, default in (("budget_usd", 4.5), ("timeout_seconds", 90)):
        value = config.get(name, default)
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            raise OpenAIStop(f"openai.{name} должен быть положительным числом.")
    maximum = config.get("max_output_tokens", 1600)
    if type(maximum) is not int or not 256 <= maximum <= 8000:
        raise OpenAIStop("openai.max_output_tokens: допустимо от 256 до 8000.")
    if config.get("reasoning_effort", "none") not in {"none", "low", "medium"}:
        raise OpenAIStop("openai.reasoning_effort: выберите none, low или medium.")


def api_key_path(config):
    return Path(config.get("_root", Path(__file__).parent)) / config.get("api_key_file", ".openai-api-key")


def load_api_key(config, *, prompt=False):
    path = api_key_path(config)
    try:
        key = path.read_text(encoding="utf-8-sig").strip() if path.exists() else os.environ.get("OPENAI_API_KEY", "").strip()
    except OSError:
        raise OpenAIStop("Не удалось прочитать локальный файл ключа OpenAI.") from None
    if not key and prompt:
        if not sys.stdin.isatty():
            raise OpenAIStop("Для скрытого ввода ключа откройте start.cmd в терминале или сохраните ключ в .openai-api-key.")
        print("\nДля OpenAI нужен API-ключ. Вставьте его в следующую строку; ввод не отображается.", flush=True)
        try:
            key = getpass.getpass("Ключ OpenAI (Enter — отмена): ").strip()
        except (EOFError, OSError):
            raise OpenAIStop("Ключ не введён. Сохраните его в .openai-api-key рядом с main.py.") from None
        if not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,}", key):
            raise OpenAIStop("Ключ не сохранён: ожидается API-ключ OpenAI, начинающийся с sk-.")
        try:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
                stream.write(key + "\n")
        except OSError:
            raise OpenAIStop("Не удалось сохранить ключ OpenAI в локальный файл.") from None
        LOG.info("Ключ OpenAI сохранён в отдельном файле. Он не выводится в журнал.")
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,}", key):
        raise OpenAIStop("Нет корректного ключа OpenAI. Запустите start.cmd и введите ключ или задайте OPENAI_API_KEY.")
    return key


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # Не передаём Authorization другому адресу при перенаправлении.


class CostLedger:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "openai-usage.sqlite3", timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, purpose TEXT, model TEXT, "
                        "reserved REAL NOT NULL, charged REAL, usage TEXT, created REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def total(self):
        return self.db.execute("SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls").fetchone()[0]

    def cached(self, key):
        row = self.db.execute("SELECT data FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def reserve(self, purpose, model, maximum, budget):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.total() + maximum > budget:
                raise OpenAIStop(f"Локальный бюджет OpenAI исчерпан: учтено ${self.total():.4f} из ${budget:.2f}. Новые платные запросы остановлены.")
            call_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO calls VALUES (?,?,?,?,NULL,NULL,?)",
                            (call_id, purpose, model, maximum, time.time()))
            self.db.commit()
            return call_id
        except BaseException:
            self.db.rollback()
            raise

    def settle(self, call_id, charge, usage=None):
        self.db.execute("UPDATE calls SET charged=?, usage=? WHERE id=?",
                        (charge, json.dumps(usage or {}), call_id))
        self.db.commit()

    def cache(self, key, data):
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?)", (key, json.dumps(data, ensure_ascii=False)))
        self.db.commit()


def usage_cost(usage, prices):
    if not isinstance(usage, dict):
        raise OpenAIStop("OpenAI не вернул учёт токенов. Резерв расходов сохранён; платные запросы остановлены.")
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    details = usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        raise OpenAIStop("Некорректные сведения о токенах OpenAI; резерв расходов сохранён.")
    cached, written = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
    if any(type(v) is not int or v < 0 for v in (incoming, outgoing, cached, written)) or cached + written > incoming:
        raise OpenAIStop("Некорректный учёт токенов OpenAI. Резерв расходов сохранён; работа остановлена.")
    return ((incoming - cached - written) * prices["input"] + cached * prices["cached"]
            + written * prices["write"] + outgoing * prices["output"]) / 1_000_000


def request_json(config, schema, instructions, data, *, purpose):
    validate_config(config)
    model = config["model"]
    maximum = config.get("max_output_tokens", 1600)
    payload = {"model": model, "store": False, "stream": False, "service_tier": "default",
               "reasoning": {"effort": config.get("reasoning_effort", "none")},
               "max_output_tokens": maximum, "instructions": instructions,
               "input": [{"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
               "text": {"format": {"type": "json_schema", "name": purpose, "strict": True, "schema": schema}}}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > 160_000:
        raise OpenAIError("Слишком длинное резюме или описание вакансии: запрос не отправлен, текст не обрезается.")
    cache_key = hashlib.sha256(encoded).hexdigest()
    prices = PRICES[model]
    # Верхняя оценка: один токен на UTF-8 байт плюс запас на служебное оформление;
    # для входа учитываем более дорогую запись кэша, для выхода — весь лимит.
    reservation = ((len(encoded) + 2048) * max(prices["input"], prices["write"])
                   + maximum * prices["output"]) / 1_000_000
    ledger = CostLedger(config.get("_data_dir", Path(__file__).parent / "output"))
    started = time.monotonic()
    try:
        cached = ledger.cached(cache_key)
        if cached is not None:
            LOG.info("OpenAI: %s | локальный кэш, новый платный запрос не требуется.", purpose)
            return {"data": cached, "cached": True, "estimated_cost_usd": 0.0,
                    "total_estimated_cost_usd": ledger.total(), "duration_seconds": 0.0, "usage": {}}
        key = load_api_key(config)
        call_id = ledger.reserve(purpose, model, reservation, config.get("budget_usd", 4.5))
        request = Request(ENDPOINT, data=encoded, headers={"Authorization": "Bearer " + key,
                          "Content-Type": "application/json"}, method="POST")
        try:
            with build_opener(NoRedirect()).open(request, timeout=config.get("timeout_seconds", 90)) as response:
                envelope = json.load(response)
        except HTTPError as exc:
            # При явной ошибке запроса/авторизации сервер не выполнил генерацию.
            if 400 <= exc.code < 500:
                ledger.settle(call_id, 0)
            messages = {401: "Ключ OpenAI не принят.", 403: "Нет доступа к OpenAI или модели.",
                        429: "OpenAI ограничил запросы или исчерпан баланс аккаунта.",
                        400: "OpenAI отклонил параметры запроса.", 404: "Модель OpenAI недоступна этому ключу."}
            raise OpenAIStop(messages.get(exc.code, f"OpenAI: HTTP {exc.code}.") + " Автоматических повторов не будет.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise OpenAIStop("Нет корректного ответа OpenAI. Стоимость запроса неизвестна: резерв сохранён, автоматического повтора не будет.") from None
        if not isinstance(envelope, dict):
            raise OpenAIStop("OpenAI вернул неизвестный формат ответа; резерв расходов сохранён.")
        charge = usage_cost(envelope.get("usage"), prices)
        ledger.settle(call_id, charge, envelope["usage"])
        LOG.info("OpenAI: %s | запрос ~$%.6f | всего учтено ~$%.4f / $%.2f", purpose, charge,
                 ledger.total(), config.get("budget_usd", 4.5))
        if envelope.get("status") != "completed":
            raise OpenAIStop("OpenAI не завершил ответ. Проверьте лимит выходных токенов; новые запросы остановлены.")
        try:
            content = [part for item in envelope.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", [])]
            if any(part.get("type") == "refusal" for part in content):
                raise OpenAIError("Модель отказалась обрабатывать вакансию; отклик не отправлен.")
            text = "".join(part["text"] for part in content if part.get("type") == "output_text")
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise OpenAIError("Модель не вернула корректный JSON; отклик не отправлен.") from None
        ledger.cache(cache_key, result)
        return {"data": result, "cached": False, "estimated_cost_usd": charge,
                "total_estimated_cost_usd": ledger.total(), "usage": envelope["usage"],
                "duration_seconds": round(time.monotonic() - started, 1)}
    finally:
        ledger.close()
