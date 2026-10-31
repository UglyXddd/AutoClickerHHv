"""Поднятие резюме, подбор вакансий и отклики через обычные страницы hh.ru."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import time
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout, expect

from hh_ui import HumanRequired, SessionRequired, check_challenge, navigate
from journal import Journal
from letters import LetterGenerationError, generate_letter
from runtime_control import CycleExpired, CycleProgress, RunControl, RunStopped
from response_guard import ResponseGuard
from openai_client import OpenAIError, OpenAIStop
from search_planner import plan_search
from resume_bumps import MOSCOW, next_bump_delay


SITE_ORIGIN = "https://hh.ru"
PROFILE_PATH = "/applicant/profile/me?hhtmFrom=services"
FREE_BUMP = re.compile(r"^(?:Поднять(?:\s+в\s+поиске|\s+вручную|\s+сейчас)?|Обновить дату)$", re.I)
QUESTION_TEXT = re.compile(
    r"для отклика необходимо ответить|(?:заполнить|заполните).{0,40}анкет|"
    r"(?:ответьте|ответить).{0,40}вопрос|пройти.{0,30}тест", re.I
)
APPLIED_TEXT = re.compile(r"Вы\s+(?:уже\s+)?откликнулись|Отклик\s+отправлен|Ваш\s+отклик.{0,30}отправлен|Резюме\s+доставлено", re.I)
TITLE_LINKS = 'a[data-qa="serp-item__title"], a[data-qa="vacancy-serp__vacancy-title"]'
EMPTY_SEARCH = re.compile(
    r"ничего\s+не\s+(?:найдено|нашлось)|не\s+найдено\s+ни\s+одной\s+вакансии|"
    r"ваканси[йи]\s+не\s+найден[оы]|найдено\s+0\s+вакансий", re.I)
LETTER_INPUT = 'textarea[data-qa="vacancy-response-popup-form-letter-input"], textarea[name="letter"]'
LOG = logging.getLogger("hh_browser")


class LetterDeliveryError(RuntimeError):
    """Продолжение рассылки недопустимо, если сайт не принял письмо."""


def no_checkpoint() -> None:
    pass


def load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        parsed = urlparse(config["main_resume_url"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Некорректный config.json: {exc}") from exc
    match = re.fullmatch(r"/resume/([0-9a-f]{32,64})", parsed.path)
    if parsed.hostname != "hh.ru" or not match:
        raise RuntimeError("main_resume_url должен иметь вид https://hh.ru/resume/<идентификатор>.")
    config["resume_id"] = match[1]
    config.setdefault("cycle_hours", 4)
    config.setdefault("minimum_score", 70)
    config.setdefault("review_again_after_hours", 24)
    config.setdefault("search_query", "python backend")
    config.setdefault("search_location", "moscow_any")
    config.setdefault("search_mode", "model")
    config.setdefault("include_lower_levels", True)
    config.setdefault("empty_search_delay_seconds", 60)
    value = config["empty_search_delay_seconds"]
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise RuntimeError("empty_search_delay_seconds должен быть положительным числом.")
    if config["search_mode"] not in {"model", "manual"}:
        raise RuntimeError("search_mode: выберите model или manual.")
    if type(config["include_lower_levels"]) is not bool:
        raise RuntimeError("include_lower_levels должен быть true или false.")
    if not isinstance(config["search_query"], str) or not config["search_query"].strip():
        raise RuntimeError("search_query должен содержать поисковый запрос.")
    if config["search_location"] not in {"moscow_any", "russia_remote"}:
        raise RuntimeError("search_location: выберите moscow_any или russia_remote.")
    if not isinstance(config.get("search_filters", {}), dict):
        raise RuntimeError("search_filters должен быть объектом с параметрами поиска.")
    for field in ("max_vacancies", "max_search_pages", "max_applications"):
        if type(config.get(field)) is not int or config[field] < 1:
            raise RuntimeError(f"{field} должен быть положительным целым числом.")
    if not isinstance(config.get("delay_seconds"), (int, float)) or config["delay_seconds"] < 0:
        raise RuntimeError("delay_seconds должен быть неотрицательным числом.")
    for field in ("cycle_hours", "review_again_after_hours"):
        value = config[field]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (field == "cycle_hours" and value == 0):
            raise RuntimeError(f"{field} должен быть допустимым числом часов.")
    if type(config["minimum_score"]) is not int or not 0 <= config["minimum_score"] <= 100:
        raise RuntimeError("minimum_score должен быть целым числом от 0 до 100.")
    config["output"] = (path.parent / config.get("output_dir", "output")).resolve()
    config["root"] = path.parent
    if "openai" in config:
        if not isinstance(config["openai"], dict):
            raise RuntimeError("openai должен быть объектом с настройками API.")
        config["openai"] = dict(config["openai"], _root=str(path.parent.resolve()), _data_dir=str(config["output"]))
    return config


def internal_url(href: str) -> str:
    url = urljoin(SITE_ORIGIN, href)
    parsed, origin = urlparse(url), urlparse(SITE_ORIGIN)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != origin.netloc:
        raise RuntimeError("Ссылка ведёт за пределы hh.ru; автоматический переход пропущен.")
    return url


def resume_id_from_card(card) -> str:
    for link in card.locator("a[href]").all():
        parsed = urlparse(link.get_attribute("href") or "")
        match = re.search(r"/resume/([0-9a-f]{32,64})", parsed.path)
        if match:
            return match[1]
        value = parse_qs(parsed.query).get("resumeHash", [""])[0]
        if value:
            return value
    return ""


def bump_all(page: Page, journal: Journal, *, dry_run: bool, checkpoint=no_checkpoint) -> float:
    checkpoint()
    navigate(page, SITE_ORIGIN + PROFILE_PATH)
    all_resumes = page.locator('[data-qa="compact-resume-show-more"]')
    href = all_resumes.get_attribute("href") if all_resumes.count() else "/applicant/my_resumes"
    checkpoint()
    navigate(page, internal_url(href))
    cards = page.locator('[data-qa="resume"]')
    cards.first.wait_for(state="attached")
    inventory = []
    delays = []

    def schedule(card, entry, *, just_bumped=False, has_free_button=False):
        now = datetime.now(MOSCOW)
        text = " ".join(card.inner_text().split())
        delay = next_bump_delay(text, now=now)
        if delay is None and just_bumped:
            delay = 4 * 3600
        if delay is None and not has_free_button and not re.search(r"поднять.{0,40}(?:вручную|в поиске)|обновить дату", text, re.I):
            return  # Скрытое/неопубликованное резюме не сбивает известные таймеры.
        check_in = 60 if delay is None else max(5, delay)
        delays.append((now + timedelta(seconds=check_in)).timestamp())
        entry["next_check_at"] = (now + timedelta(seconds=check_in)).isoformat()
        journal.event("bump_scheduled", note=f'{entry["title"]}: проверю {now + timedelta(seconds=check_in):%d.%m в %H:%M:%S} МСК',
                      resume_id=entry["id"], next_check_at=entry["next_check_at"], delay_seconds=check_in)
    for index in range(cards.count()):
        checkpoint()
        card = cards.nth(index)
        title = card.locator('[data-qa="resume-title"]').first.inner_text()
        resume_id = resume_id_from_card(card)
        entry = {"id": resume_id, "title": title}
        inventory.append(entry)
        button = card.get_by_role("button", name=FREE_BUMP).or_(card.get_by_role("link", name=FREE_BUMP)).first
        if not button.count() or not button.is_visible() or not button.is_enabled():
            journal.event("bump_unavailable", note=f"{title}: {card.inner_text()[:280]}")
            schedule(card, entry, has_free_button=bool(button.count()))
            continue
        if dry_run:
            journal.event("would_bump", note=title)
            delays.append(datetime.now(MOSCOW).timestamp() + 60)
            continue
        checkpoint()
        button.click()
        check_challenge(page)
        try:
            expect(card).to_contain_text(
                re.compile(r"Поднять.*(?:вручную можно|в\s*\d|через)|(?:резюме|дата).*обновл[её]н", re.I | re.S),
                timeout=7000,
            )
            journal.event("resume_bumped", note=title, resume_id=resume_id)
            schedule(card, entry, just_bumped=True)
        except AssertionError:
            journal.event("bump_unconfirmed", note=f"{title}: сайт не показал подтверждение поднятия")
            delays.append(datetime.now(MOSCOW).timestamp() + 60)
    (journal.directory / "resumes.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return max(5, min(delays) - datetime.now(MOSCOW).timestamp()) if delays else 60


def search_url(config: dict) -> str:
    filters = dict(config.get("search_filters", {}))
    for key in ("resume", "from", "work_format", "schedule"):
        filters.pop(key, None)
    filters.setdefault("search_field", "name")
    filters.setdefault("order_by", "publication_time")
    # hh.ru отдаёт первые 20 карточек сразу; более крупные страницы требуют
    # дополнительной подгрузки. Небольшие страницы не теряют эти вакансии.
    filters["items_on_page"] = 20
    filters["text"] = config["search_query"].strip()
    filters["area"] = "1" if config["search_location"] == "moscow_any" else "113"
    if config["search_location"] == "russia_remote":
        filters["work_format"] = "REMOTE"
    return SITE_ORIGIN + "/search/vacancy?" + urlencode(filters, doseq=True)


def collect_vacancies(page: Page, config: dict, *, journal: Journal | None = None,
                      signature: str = "", checkpoint=no_checkpoint, seen_ids=None) -> list[dict]:
    levels = config.get("search_filters", {}).get("experience")
    if isinstance(levels, list) and len(levels) > 1:
        # Один диапазон на URL: не полагаемся на поддержку нескольких experience
        # сайтом. Чередуем страницы диапазонов, сохраняя общий предел вакансий.
        urls = [search_url(dict(config, search_filters=dict(config["search_filters"], experience=level)))
                for level in reversed(levels)]
    else:
        urls = [search_url(config)]
    pending = deque(dict.fromkeys(urls))
    if journal:
        for url in pending:
            journal.event("search_started", note=url, search_location=config["search_location"])
    found = {}
    visited = set()
    skipped = 0
    for page_number in range(config["max_search_pages"]):
        checkpoint()
        if not pending:
            break
        current_url = pending.popleft()
        if current_url in visited:
            continue
        visited.add(current_url)
        navigate(page, current_url)
        links = page.locator(TITLE_LINKS)
        while True:
            try:
                # Ждём карточки либо пустую выдачу, в том числе после CAPTCHA.
                page.wait_for_function("""({titles, empty}) => {
                    if (document.querySelector(titles)) return true;
                    const body = (document.body?.innerText || '').replace(/\\s+/g, ' ');
                    return new RegExp(empty, 'i').test(body);
                }""", arg={"titles": TITLE_LINKS, "empty": EMPTY_SEARCH.pattern})
                break
            except PlaywrightTimeout:
                if not check_challenge(page):
                    break
        if not links.count():
            if any(item.is_visible() for item in page.get_by_text(EMPTY_SEARCH).all()):
                if journal:
                    journal.event("search_empty", note=current_url)
                continue
            check_challenge(page)
            if not links.count():
                note = "Не удалось распознать выдачу вакансий."
                if journal:
                    diagnostic = journal.directory / "search-error.json"
                    diagnostic.write_text(json.dumps({"url": page.url, "title": page.title(),
                        "headings": page.get_by_role("heading").all_inner_texts()[:12],
                        "vacancy_links": page.locator('a[href*="/vacancy/"]').count()},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                    note += f" Диагностика сохранена: {diagnostic}"
                raise RuntimeError(note)
        entries = links.evaluate_all("links => links.map(link => ({href: link.href, title: link.innerText}))")
        for entry in entries:
            try:
                url = internal_url(entry["href"])
            except RuntimeError:
                continue  # Рекламные ссылки могут вести на adsrv.hh.ru.
            match = re.fullmatch(r"/vacancy/(\d+)", urlparse(url).path)
            if match:
                vacancy_id = match[1]
                if seen_ids and vacancy_id in seen_ids:
                    skipped += 1
                    continue
                if journal:
                    status = journal.status(vacancy_id)
                    if status and status != "questionnaire_pending_favorite":
                        skipped += 1
                        continue
                    if signature and journal.reviewed_recently(vacancy_id, signature, config["review_again_after_hours"]):
                        skipped += 1
                        continue
                found[vacancy_id] = {"id": vacancy_id, "title": entry["title"],
                                     "url": SITE_ORIGIN + "/vacancy/" + vacancy_id}
            if len(found) >= config["max_vacancies"]:
                if journal:
                    journal.event("search_page", note=f"Страница {page_number + 1}; в очереди всего {len(found)}; по локальной истории пропущено всего {skipped}")
                return list(found.values())
        if journal:
            journal.event("search_page", note=f"Страница {page_number + 1}; в очереди всего {len(found)}; по локальной истории пропущено всего {skipped}")
        next_page = page.locator('a[data-qa="pager-next"]').first
        href = next_page.get_attribute("href") if next_page.count() and next_page.is_visible() else None
        if not href:
            current_index = int(parse_qs(urlparse(page.url).query).get("page", ["0"])[0])
            for link in page.locator('a[data-qa="pager-page"]').all():
                candidate = link.get_attribute("href") or ""
                index = parse_qs(urlparse(candidate).query).get("page", [""])[0]
                if index == str(current_index + 1):
                    href = candidate
                    break
        if href:
            pending.append(internal_url(href))
    return list(found.values())


def questionnaire_present(page: Page) -> bool:
    if page.get_by_text(QUESTION_TEXT).first.is_visible():
        return True
    form = page.locator('form[name="vacancy_response"]')
    if not form.count():
        return page.locator('[data-qa*="questionnaire"], [data-qa*="applicant-test"]').first.is_visible()
    extra_fields = form.locator(
        'textarea:visible:not([data-qa="vacancy-response-popup-form-letter-input"]):not([name="letter"]), '
        'input:visible:not([type="hidden"]):not([type="submit"]), select:visible'
    )
    return extra_fields.count() > 0


def save_questionnaire(page: Page, vacancy: dict, journal: Journal, *, dry_run: bool,
                       checkpoint=no_checkpoint) -> None:
    if dry_run:
        journal.event("would_favorite_questionnaire", vacancy=vacancy)
        return
    journal.mark(vacancy, "questionnaire_pending_favorite")
    checkpoint()
    navigate(page, vacancy["url"])
    added = page.locator('[data-qa="vacancy-body-mark-favorite_true"]').first
    if not added.is_visible():
        button = page.locator('[data-qa="vacancy-body-mark-favorite_false"]').first
        button.wait_for(state="visible")
        checkpoint()
        button.click()
        added.wait_for(state="visible")
    journal.mark(vacancy, "questionnaire", "В избранном; анкету нужно заполнить позже.")


def read_resume(page: Page, config: dict, *, checkpoint=no_checkpoint) -> tuple[str, str]:
    checkpoint()
    navigate(page, SITE_ORIGIN + "/resume/" + config["resume_id"])
    title = page.locator('[data-qa="resume-block-title-position"], h1').first.inner_text()
    sections = page.locator(
        '[data-qa="resume-list-card-experience"], [data-qa="skills-card"], '
        '[data-qa="resume-about-card"], [data-qa="resume-block-experience"], '
        '[data-qa="resume-block-skills"], [data-qa="resume-block-skill-description"]'
    ).all_inner_texts()
    if not sections:
        raise RuntimeError("Не удалось прочитать профессиональные разделы основного резюме.")
    return title, title + "\n\n" + "\n\n".join(sections)


def choose_resume(page: Page, resume_id: str, resume_title: str, *, checkpoint=no_checkpoint) -> None:
    form = page.locator('form[name="vacancy_response"]')
    selected = form.locator('[data-qa="resume-title"]').first
    checkpoint()
    selected.wait_for(state="visible")
    if selected.inner_text().strip() != resume_title.strip():
        selected.click()
        radio = page.locator(f'input[type="radio"][value="{resume_id}"]')
        radio.wait_for(state="attached", timeout=5000)
        checkpoint()
        radio.check()
    expect(selected).to_have_text(resume_title)
    try:
        expect(form.locator('[data-qa="hidden-resume-warning"]')).not_to_be_visible(timeout=3000)
    except AssertionError as exc:
        raise RuntimeError("Сайт требует изменить видимость резюме. Проверьте настройку вручную.") from exc


def instructions_from_config(config: dict) -> str:
    filename = config.get("letter_instructions_file")
    if not filename:
        raise RuntimeError(
            "Формат сопроводительного письма пока не задан. Укажите letter_instructions_file "
            "в config.json. Поднятие и поиск доступны через --task bump / search."
        )
    instructions = (config["root"] / filename).read_text(encoding="utf-8-sig").strip()
    if not instructions:
        raise RuntimeError("Файл с форматом письма пустой; отправка не начата.")
    return instructions


def already_applied(page: Page) -> bool:
    return page.locator("main").get_by_text(APPLIED_TEXT).first.is_visible()


def wait_response_form(page: Page, guard: ResponseGuard, checkpoint, *, allow_form=True) -> None:
    stop_at = time.monotonic() + 15
    form = page.locator('form[name="vacancy_response"]')
    while time.monotonic() < stop_at:
        checkpoint()
        if check_challenge(page):
            if guard.sent or guard.preview:
                return
            stop_at = time.monotonic() + 15
            continue
        if guard.failure:
            raise guard.failure
        if guard.blocked or guard.page_required or guard.preview or guard.sent or (allow_form and form.is_visible()):
            return
        page.wait_for_timeout(100)
    check_challenge(page)
    raise RuntimeError("Сайт не открыл форму и не начал проверенный запрос отклика.")


def send_new_response(page: Page, vacancy: dict, config: dict, journal: Journal,
                      resume_title: str, result: dict, *, dry_run: bool, checkpoint) -> str:
    """Открывает интерфейс; быстрый отклик также проходит проверку письма в запросе."""
    letters = journal.directory / "letters"
    letters.mkdir(exist_ok=True)
    (letters / (vacancy["id"] + ".txt")).write_text(result["letter"], encoding="utf-8")
    journal.event("letter_prepared", vacancy=vacancy, note=f'{len(result["letter"])} символов; основное резюме: {resume_title}')
    with ResponseGuard(page, SITE_ORIGIN, vacancy["id"], config["resume_id"], result["letter"],
                       dry_run=dry_run, reserve=lambda: journal.reserve(vacancy), checkpoint=checkpoint) as guard:
        guard.active = True
        try:
            response_link = page.locator('[data-qa="vacancy-response-link-top"]').first
            checkpoint()
            response_link.click()
            wait_response_form(page, guard, checkpoint)
            if guard.page_required:
                # Отдельная страница анкеты; опасный прямой GET не выполнялся.
                return "questionnaire"
            if guard.blocked:
                journal.event("response_blocked", vacancy=vacancy, note=guard.blocked)
                return "blocked"
            form = page.locator('form[name="vacancy_response"]')
            if not guard.sent and not guard.preview:
                if questionnaire_present(page):
                    return "questionnaire"
                choose_resume(page, config["resume_id"], resume_title, checkpoint=checkpoint)
                if questionnaire_present(page):
                    return "questionnaire"
                textarea = form.locator(LETTER_INPUT).first
                if not textarea.is_visible():
                    checkpoint()
                    form.locator('[data-qa="vacancy-response-letter-toggle"]').click()
                checkpoint()
                textarea.fill(result["letter"])
                expect(textarea).to_have_value(result["letter"])
                if questionnaire_present(page):
                    return "questionnaire"
                if dry_run:
                    journal.event("would_apply", vacancy=vacancy, score=result["score"], letter=result["letter"])
                    return "preview"
                submit = form.locator('[data-qa="vacancy-response-submit-popup"]')
                expect(submit).to_be_enabled()
                checkpoint()
                submit.click()
                wait_response_form(page, guard, checkpoint, allow_form=False)
            if guard.failure:
                raise guard.failure
            if guard.blocked:
                journal.event("response_blocked", vacancy=vacancy, note=guard.blocked)
                return "blocked"
            if guard.preview:
                journal.event("would_apply", vacancy=vacancy, note="Быстрый отклик перехвачен; в запрос добавлено письмо. Отправка отключена.",
                              score=result["score"], letter=result["letter"])
                return "preview"
            if not guard.sent:
                raise RuntimeError("Браузер не отправил проверенный запрос отклика.")
            journal.event("application_request_verified", vacancy=vacancy, note=f'Резюме {config["resume_id"]}; письмо {len(result["letter"])} символов',
                          resume_id=config["resume_id"], letter_chars=len(result["letter"]), quick_response=guard.rewritten)
            try:
                page.get_by_text(APPLIED_TEXT).first.wait_for(timeout=7000)
            except PlaywrightTimeout:
                check_challenge(page)
            # Подтверждение читаем заново с сервера: локальное состояние быстрого
            # отклика не знает о добавленном в его запрос письме.
            navigate(page, vacancy["url"])
            if not already_applied(page):
                raise RuntimeError("После отправки сайт не подтвердил отклик.")
            attach = page.locator('[data-qa="responded-success-attach-cover-letter"]').first
            if attach.is_visible():
                raise LetterDeliveryError("Сайт принял резюме, но предлагает добавить письмо. Рассылка остановлена; проверьте последний отклик.")
            journal.mark(vacancy, "applied", result["reason"], score=result["score"], letter=result["letter"])
            return "applied"
        finally:
            if journal.status(vacancy["id"]) == "submission_pending":
                journal.mark(vacancy, "submission_unknown", "Подтверждение письма не получено. Проверьте отклик вручную; повторной отправки не будет.")


def apply_one(page: Page, vacancy: dict, config: dict, journal: Journal,
              resume_title: str, resume_text: str, instructions: str, *, dry_run: bool,
              signature: str = "", checkpoint=no_checkpoint) -> bool:
    checkpoint()
    previous = journal.status(vacancy["id"])
    if previous == "questionnaire_pending_favorite":
        save_questionnaire(page, vacancy, journal, dry_run=dry_run, checkpoint=checkpoint)
        return False
    if previous:
        journal.event("skipped_history", vacancy=vacancy, note=previous)
        return False
    navigate(page, vacancy["url"])
    journal.event("vacancy_opened", vacancy=vacancy)
    if already_applied(page):
        if not dry_run:
            journal.mark(vacancy, "already_applied")
        return False
    description = page.locator('[data-qa="vacancy-description"]').inner_text()
    checkpoint()
    journal.event("model_started", vacancy=vacancy, note=config["openai"]["model"])
    result = generate_letter(config["openai"], instructions, resume_text, vacancy["title"] + "\n" + description)
    journal.event("vacancy_evaluated", vacancy=vacancy, score=result["score"], note=result["reason"],
                  duration_seconds=result.get("duration_seconds"), cached=result.get("cached"),
                  estimated_cost_usd=result.get("estimated_cost_usd"), usage=result.get("usage"))
    if not result["suitable"] or result["score"] < config["minimum_score"]:
        journal.event("not_suitable", vacancy=vacancy, score=result["score"], note=result["reason"])
        if not dry_run:
            journal.record_review(vacancy, signature, result["score"], result["reason"])
        return False
    if result.get("missing_requirements"):
        reason = "; ".join(result["missing_requirements"])
        journal.event("requirements_missing", vacancy=vacancy, score=result["score"], note=reason)
        if not dry_run:
            journal.record_review(vacancy, signature, result["score"], reason)
        return False
    checkpoint()
    if already_applied(page):
        if not dry_run:
            journal.mark(vacancy, "already_applied")
        return False
    response_link = page.locator('[data-qa="vacancy-response-link-top"]').first
    if not response_link.count():
        journal.event("unsupported_response", vacancy=vacancy, note="Нет обычной формы отклика hh.ru.")
        return False
    href = response_link.get_attribute("href")
    if href and urlparse(internal_url(href)).path != "/applicant/vacancy_response":
        journal.event("unsupported_response", vacancy=vacancy, note="Необычная ссылка отклика.")
        return False
    outcome = send_new_response(page, vacancy, config, journal, resume_title, result,
                                dry_run=dry_run, checkpoint=checkpoint)
    if outcome == "questionnaire":
        save_questionnaire(page, vacancy, journal, dry_run=dry_run, checkpoint=checkpoint)
    return outcome == "applied"


def run_actions(page: Page, *, task: str = "bump", config_path: Path | None = None,
                dry_run: bool = False, control: RunControl | None = None,
                cycle_started: float | None = None, deadline: float | None = None,
                on_human_required=None, progress: CycleProgress | None = None) -> int | float:
    config = load_config(config_path or Path(__file__).with_name("config.json"))
    control = control or RunControl()
    progress = progress or CycleProgress(config["max_vacancies"], config["max_applications"])
    checkpoint = lambda: control.checkpoint(deadline)
    journal = Journal(config["output"])
    checked = sent = 0

    def perform(operation):
        nonlocal page
        while True:
            checkpoint()
            try:
                return operation(page)
            except (HumanRequired, PlaywrightError) as exc:
                if not isinstance(exc, HumanRequired):
                    try:
                        check_challenge(page)
                    except HumanRequired as challenge:
                        exc = challenge
                    else:
                        raise
                if on_human_required is None or exc.kind != "captcha":
                    raise exc
                page = on_human_required(exc)

    try:
        journal.event("run_started", note=f"task={task}; dry_run={dry_run}")
        if task in {"bump", "all"}:
            next_bump = perform(lambda active: bump_all(active, journal, dry_run=dry_run, checkpoint=checkpoint))
        if task == "bump":
            return next_bump
        if progress.full:
            return 0
        instructions = instructions_from_config(config) if task in {"apply", "all"} else ""
        signature = ""
        if task in {"apply", "all"} or config["search_mode"] == "model":
            resume_title, resume_text = perform(lambda active: read_resume(active, config, checkpoint=checkpoint))
        if task in {"apply", "all"}:
            signature = hashlib.sha256(json.dumps(
                [resume_text, instructions, config["minimum_score"], config["openai"]],
                ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")).hexdigest()
        if config["search_mode"] == "model":
            checkpoint()
            journal.event("search_planning", note=config["openai"]["model"])
            search_context = hashlib.sha256(json.dumps(
                [resume_text, config["search_location"], config["include_lower_levels"], config["openai"]["model"]],
                ensure_ascii=False).encode("utf-8")).hexdigest()
            previous_queries = journal.recent_search_queries(search_context)
            round_id = journal.begin_search_round(search_context)
            plan = plan_search(config["openai"], resume_text, location=config["search_location"],
                               include_lower=config["include_lower_levels"],
                               previous_queries=previous_queries, round_number=round_id)
            checkpoint()
            journal.record_search_plan(round_id, plan)
            config["search_query"] = plan["query"]
            config["search_filters"] = dict(config.get("search_filters", {}),
                                            search_field=plan["search_field"], experience=plan["experience"])
            (journal.directory / "search-plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            journal.event("search_planned", note=f'{plan["query"]}; уровень {plan["level"]}; опыт {", ".join(plan["experience"])}. {plan["reason"]}',
                          plan=plan)
        search_config = dict(config, max_vacancies=progress.max_vacancies - progress.checked)
        vacancies = perform(lambda active: collect_vacancies(active, search_config, journal=journal,
                                                            signature=signature, checkpoint=checkpoint,
                                                            seen_ids=progress.seen_ids))
        (journal.directory / "vacancies.json").write_text(
            json.dumps(vacancies, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        journal.event("vacancies_found", note=str(len(vacancies)))
        if task == "search":
            progress.checked += len(vacancies)
            progress.seen_ids.update(vacancy["id"] for vacancy in vacancies)
            return len(vacancies)
        for vacancy in vacancies:
            if progress.full:
                break
            if cycle_started is not None and deadline is not None:
                slot = cycle_started + progress.checked * (deadline - cycle_started) / progress.max_vacancies
                control.wait_until(slot, deadline=deadline)
            checkpoint()
            checked += 1
            progress.checked += 1
            progress.seen_ids.add(vacancy["id"])
            try:
                applied = perform(lambda active: apply_one(active, vacancy, config, journal, resume_title, resume_text,
                                                           instructions, dry_run=dry_run, signature=signature, checkpoint=checkpoint))
                sent += applied
                progress.sent += applied
            except (CycleExpired, RunStopped, HumanRequired, SessionRequired, LetterDeliveryError, OpenAIStop):
                raise
            except (LetterGenerationError, OpenAIError) as exc:
                journal.event("model_error", vacancy=vacancy, note=str(exc))
                control.wait_until(control.now() + 60, deadline=deadline)
            except PlaywrightError as exc:
                check_challenge(page)
                journal.event("ui_error", vacancy=vacancy, note=str(exc)[:400])
            except (RuntimeError, AssertionError) as exc:
                journal.event("vacancy_error", vacancy=vacancy, note=str(exc)[:600])
            control.wait_until(control.now() + config["delay_seconds"], deadline=deadline)
        journal.event("run_finished", note=f"Проверено: {checked}; подтверждённых откликов: {sent}",
                      checked=checked, sent=sent)
        return len(vacancies)
    except CycleExpired:
        journal.event("cycle_expired", note=f"Проверено: {checked}; подтверждённых откликов: {sent}",
                      checked=checked, sent=sent)
        raise
    finally:
        journal.close()
