"""Проверка запроса отклика до его отправки браузером."""

from __future__ import annotations

import json
import re
from email import policy
from email.parser import BytesParser
from contextvars import ContextVar
from urllib.parse import parse_qs, urlencode, urlparse
from playwright.sync_api import Error as PlaywrightError

from hh_ui import HumanRequired, check_challenge

ACTIVE_RESPONSE_GUARD = ContextVar("active_response_guard", default=None)


def read_fields(content_type: str, body: bytes) -> dict[str, list[str]]:
    if "multipart/form-data" in content_type:
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        if not message.is_multipart():
            raise ValueError("Не удалось разобрать поля отклика.")
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name or part.get_filename():
                raise ValueError("Неизвестное поле или вложение в отклике.")
            value = (part.get_payload(decode=True) or b"").decode("utf-8")
            fields.setdefault(name, []).append(value)
        return fields
    if "application/json" in content_type:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Ожидался объект с полями отклика.")
        return {key: [str(value)] for key, value in data.items()}
    if "application/x-www-form-urlencoded" in content_type:
        return parse_qs(body.decode("utf-8"), keep_blank_values=True)
    raise ValueError("Неизвестный формат запроса отклика.")


def add_letter(content_type: str, body: bytes, letter: str) -> bytes:
    """Добавляет письмо к быстрому отклику, сохраняя остальные поля браузера."""
    if "multipart/form-data" in content_type:
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + content_type + "\r\n\r\n").encode())
        boundary = message.get_boundary()
        if not boundary or not re.fullmatch(r"[\w'()+,./:=?-]{1,200}", boundary):
            raise ValueError("Некорректная граница полей отклика.")
        marker = b"--" + boundary.encode("ascii")
        # В новой форме CAPTCHA фронтенд иногда присылает пустой letter.
        # Заменяем только это поле; cookies, XSRF и ответ человека не меняем.
        body = marker.join(part for part in body.split(marker) if not re.search(
            br'name="letter"(?:;|\r\n|$)', part.partition(b"\r\n\r\n")[0]))
        closing = body.rfind(marker + b"--")
        if closing < 0:
            raise ValueError("Не найдена завершающая граница отклика.")
        part = marker + b'\r\nContent-Disposition: form-data; name="letter"\r\n\r\n'
        return body[:closing] + part + letter.encode("utf-8") + b"\r\n" + body[closing:]
    if "application/json" in content_type:
        data = json.loads(body)
        data["letter"] = letter
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    fields["letter"] = [letter]
    return urlencode(fields, doseq=True).encode("utf-8")


class ResponseGuard:
    """Пропускает один новый отклик, только с нужным резюме и готовым письмом."""

    def __init__(self, page, origin: str, vacancy_id: str, resume_id: str, letter: str,
                 *, dry_run: bool, reserve, checkpoint):
        self.page, self.origin = page, urlparse(origin).netloc
        self.vacancy_id, self.resume_id, self.letter = vacancy_id, resume_id, letter
        self.dry_run, self.reserve, self.checkpoint = dry_run, reserve, checkpoint
        self.active = False
        self.request_seen = self.sent = self.preview = self.page_required = False
        self.blocked = ""
        self.failure = None
        self.rewritten = False
        self.response = None
        self.reserved = False
        self.captcha_submission = False
        self.captcha_checkpoint = None
        self.handler = self._route
        self.observer = self._response

    def __enter__(self):
        self._context_token = ACTIVE_RESPONSE_GUARD.set(self)
        self.page.route("**/*", self.handler)
        self.page.on("response", self.observer)
        return self

    def __exit__(self, _exc_type, exc, _traceback):
        challenge = exc if isinstance(exc, HumanRequired) and exc.kind == "captcha" else None
        try:
            # Убираем документ и его отложенные повторы запроса, пока защита
            # ещё включена. Особенно важно после отменённого dry-run.
            if not self.page.is_closed():
                if (exc is None and not self.sent) or isinstance(exc, (PlaywrightError, AssertionError)):
                    try:
                        check_challenge(self.page)
                    except HumanRequired as detected:
                        challenge = detected
                    except PlaywrightError:
                        pass
                self.active = False
                if challenge is None and (self.dry_run or self.blocked or exc is not None):
                    self.page.goto("about:blank", wait_until="commit")
        finally:
            self.active = False
            self.page.unroute("**/*", self.handler)
            self.page.remove_listener("response", self.observer)
            ACTIVE_RESPONSE_GUARD.reset(self._context_token)
        if challenge:
            raise challenge

    def prepare_captcha_retry(self):
        if not self.sent:
            return
        response = self.response
        rejected = response is not None and response.status in {400, 401, 403, 422}
        if response is not None and not rejected:
            try:
                data = response.json()
                errors = {key: data[key] for key in ("error", "errors", "errorCode", "captcha_required", "captchaRequired") if key in data}
                rejected = bool(errors) and "captcha" in json.dumps(errors).casefold()
            except (ValueError, TypeError, PlaywrightError):
                pass
        if not rejected:
            raise RuntimeError("Результат предыдущей отправки не подтверждён как отказ CAPTCHA; повтор заблокирован.")
        # Сервер явно отверг предыдущую попытку. Та же резервная запись и то же
        # письмо используются после ответа человека; успешный отклик не повторяем.
        self.sent = False
        self.blocked = ""
        self.response = None

    def _response(self, response):
        if response.request.method == "POST" and urlparse(response.url).path in {
            "/applicant/vacancy_response", "/applicant/vacancy_response/popup",
        }:
            self.response = response

    def _route(self, route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.netloc != self.origin or not parsed.path.startswith("/applicant/vacancy_response"):
            if self.captcha_submission and re.search(r"captcha|challenge", parsed.path, re.I):
                route.fallback()
                return
            if parsed.netloc == self.origin and self.active and request.method not in {"GET", "HEAD", "OPTIONS"} and parsed.path not in {
                "/anatskytics", "/shards/stat", "/shards/vacancy/register_interaction",
            }:
                self.blocked = "Неизвестный запрос при открытии/отправке отклика: " + parsed.path
                route.abort()
                return
            route.fallback()
            return
        if request.method in {"GET", "HEAD"} and parsed.path == "/applicant/vacancy_response/popup":
            route.fallback()
            return
        if request.method == "GET" and request.resource_type == "image" and re.search(r"captcha", parsed.path, re.I):
            # CAPTCHA может загружаться из подпути формы. Это изображение,
            # а не команда отклика; защита не должна обрывать его загрузку.
            route.fallback()
            return
        if request.method == "GET" and parsed.path == "/applicant/vacancy_response":
            # Прямой переход способен отправить быстрый отклик без формы и письма.
            self.page_required = True
            route.abort()
            return
        if request.method != "POST" or parsed.path not in {
            "/applicant/vacancy_response", "/applicant/vacancy_response/popup",
        }:
            self.blocked = "Неизвестный способ отправки отклика."
            route.abort()
            return
        self.request_seen = True
        try:
            checkpoint = self.captcha_checkpoint if self.captcha_submission else None
            (checkpoint or self.checkpoint)()
            content_type = request.headers.get("content-type", "")
            body = request.post_data_buffer or b""
            fields = read_fields(content_type, body)
            if self.captcha_submission and any(value.strip() for name in ("captchaText", "captcha") for value in fields.get(name, [])):
                # Человек может подтвердить CAPTCHA в видимом браузере.
                # Повтор по-прежнему требует явного отказа предыдущей попытки.
                self.prepare_captcha_retry()
            if not self.active or self.sent or self.preview:
                raise ValueError("Повторная или неподготовленная отправка отклика заблокирована.")
            for names, expected in (
                (("resume_hash", "resume_id", "resumeId", "resumeHash"), self.resume_id),
                (("vacancy_id", "vacancyId"), self.vacancy_id),
            ):
                values = [value for name in names for value in fields.get(name, [])]
                if not values or any(value != expected for value in values):
                    raise ValueError("В запросе отклика другое резюме или другая вакансия.")
            if any(re.match(r"task_|question|answer", name, re.I) for name in fields) or any(
                value.lower() in {"true", "1"} for value in fields.get("testRequired", [])
            ):
                raise ValueError("Обнаружены ответы на анкету; автоматическая отправка запрещена.")
            if not self.letter.strip():
                raise ValueError("Пустое сопроводительное письмо.")
            if "letter" not in fields or not any(value.strip() for value in fields["letter"]):
                body = add_letter(content_type, body, self.letter)
                fields = read_fields(content_type, body)
                self.rewritten = True
            normalize = lambda value: value.replace("\r\n", "\n")
            if fields.get("letter") != [self.letter] and [normalize(x) for x in fields.get("letter", [])] != [normalize(self.letter)]:
                raise ValueError("Письмо в запросе не совпадает с подготовленным текстом.")
            if self.dry_run:
                self.preview = True
                route.abort()
                return
            if not self.reserved and not self.reserve():
                raise ValueError("Отклик уже есть в локальной истории.")
            self.reserved = True
            self.sent = True
            route.fallback(post_data=body)
        except (ValueError, UnicodeError) as exc:
            self.blocked = str(exc)
            route.abort()
        except BaseException as exc:
            self.failure = exc
            route.abort()
