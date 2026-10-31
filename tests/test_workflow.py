"""Сквозные проверки действий в настоящем браузере на перехваченных локальных страницах."""

import io
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse, urlencode

from playwright.sync_api import sync_playwright

import actions
from journal import Journal
from letters import generate_letter
from runtime_control import CycleExpired, CycleProgress
from response_guard import read_fields
from hh_ui import HumanRequired
from hh_ui import captcha_handler
from captcha_relay import CaptchaRelay


RESUME_ID = "8b282e8cff0d8bdb180039ed1f413839487a4b"
RESUME_TITLE = "Python Backend-Разработчик"
OTHER_ID = "96dfc28fff11380a920039ed1f6c7451646e79"
LETTER = "Здравствуйте!\n\nМой основной стек — Python, FastAPI, PostgreSQL.\n\nМеня заинтересовали задачи разработки API, с которыми у меня есть опыт.\n\nБуду рад обсудить детали."
MODEL_PARTS = {"suitable": True, "score": 85, "reason": "Подходит", "stack": ["Python", "FastAPI", "PostgreSQL"],
               "interest": "Меня заинтересовали задачи разработки API, с которыми у меня есть опыт.", "requirements": []}
MODEL_RESUME = "Основной стек Python, FastAPI, PostgreSQL. Есть опыт разработки API."


class FastControl:
    def __init__(self):
        self.current = 0
        self.waits = []

    def now(self):
        return self.current

    def checkpoint(self, deadline=None):
        if deadline is not None and self.current >= deadline:
            raise CycleExpired()

    def wait_until(self, target, *, deadline=None):
        self.checkpoint(deadline)
        if target > self.current:
            self.waits.append(target)
        self.current = max(self.current, target)
        self.checkpoint(deadline)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            channel=os.environ.get("HH_TEST_BROWSER", "msedge" if os.name == "nt" else "chromium"),
            headless=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="hh-workflow-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "main_resume_url": "https://hh.ru/resume/" + RESUME_ID,
            "output_dir": "output", "max_vacancies": 3, "max_search_pages": 2,
            "max_applications": 10, "delay_seconds": 0,
            "openai": {"model": "test"}, "search_mode": "manual",
            "letter_instructions_file": "style.txt",
        }
        self.config_path = self.root / "config.json"
        self.write_config()
        (self.root / "style.txt").write_text("Короткое письмо по подтверждённому опыту.", encoding="utf-8")
        self.submissions = []
        self.favorites = set()
        self.bumps = []
        self.paid_clicks = 0
        self.confirm_submission = True
        self.quick_response = False
        self.quick_retry = False
        self.quick_resume = RESUME_ID
        self.ignore_letter = False
        self.captcha_after_submit = False
        self.text_captcha_required = False
        self.captcha_attempts = []
        self.search_pages = {0: [1, 2, 3]}
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route("http://hh.local/**", self.route)
        self.page = self.context.new_page()
        self.page.set_default_timeout(3000)
        self.origin_patch = patch.object(actions, "SITE_ORIGIN", "http://hh.local")
        self.origin_patch.start()
        self.addCleanup(self.origin_patch.stop)
        self.model_patch = patch.object(actions, "generate_letter", return_value={
            "suitable": True, "score": 85, "reason": "Совпадает профессиональный стек.", "letter": LETTER,
            "requirements": [], "missing_requirements": [],
        })
        self.model = self.model_patch.start()
        self.addCleanup(self.model_patch.stop)

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def route(self, route):
        parsed = urlparse(route.request.url)
        query = parse_qs(parsed.query)
        path = parsed.path
        body = ""
        if path == "/applicant/profile/me":
            body = '<a data-qa="compact-resume-show-more" href="/applicant/my_resumes">Все резюме</a>'
        elif path == "/applicant/my_resumes":
            body = f'''<div data-qa="resume"><div data-qa="resume-title">{RESUME_TITLE}</div>
            <a href="/resume/{RESUME_ID}">Открыть</a>
            <button onclick="fetch('/bump').then(()=>this.outerHTML='Поднять вручную можно сегодня в 18:00')">Поднять в поиске</button>
            <button onclick="fetch('/paid')">Поднять автоматически</button></div>
            <div data-qa="resume"><div data-qa="resume-title">Второе резюме</div>
            <a href="/resume/{OTHER_ID}">Открыть</a><div>Поднять вручную можно сегодня в 19:00</div>
            <button onclick="fetch('/paid')">Поднять автоматически</button></div>
            <div data-qa="resume"><div data-qa="resume-title">Скрытое резюме</div></div>'''
        elif path == "/bump":
            self.bumps.append("bumped")
        elif path == "/paid":
            self.paid_clicks += 1
        elif path == "/search/vacancy":
            self.assertNotIn("resume", query)
            self.assertNotIn("from", query)
            self.assertEqual(query["text"], ["python backend"])
            self.assertEqual(query["area"], ["1"])
            page_index = int(query.get("page", [0])[0])
            body = "".join(f'<a data-qa="serp-item__title" href="/vacancy/{i}">Вакансия {i}</a>'
                           for i in self.search_pages[page_index])
            if page_index + 1 in self.search_pages:
                next_query = dict(query, page=[str(page_index + 1)])
                body += f'<a data-qa="pager-page" href="/search/vacancy?{urlencode(next_query, doseq=True)}">{page_index + 2}</a>'
        elif path == "/resume/" + RESUME_ID:
            body = f'''<h1 data-qa="resume-block-title-position">{RESUME_TITLE}</h1>
            <div data-qa="resume-list-card-experience">Разработка на Python и FastAPI.</div>
            <div data-qa="skills-card">Python PostgreSQL</div>
            <div data-qa="resume-contacts-phone">PRIVATE_CONTACT</div>'''
        elif path.startswith("/vacancy/"):
            job_id = path.rsplit("/", 1)[1]
            favorite = "true" if job_id in self.favorites else "false"
            applied = self.confirm_submission and any(x["id"] == job_id for x in self.submissions)
            click = f"event.preventDefault(); fetch('/applicant/vacancy_response/popup?vacancyId={job_id}').then(r=>r.text()).then(t=>document.body.insertAdjacentHTML('beforeend',t))"
            if job_id == "2":
                click = f"location.href='/applicant/vacancy_response?vacancyId={job_id}&startedWithQuestion=false'; event.preventDefault()"
            if self.quick_response and job_id == "1":
                click = f"event.preventDefault(); const f=new FormData(); f.set('resume_hash','{self.quick_resume}'); f.set('vacancy_id','{job_id}'); f.set('letterRequired','false'); fetch('/applicant/vacancy_response/popup',{{method:'POST',body:f}}).then(r=>r.text()).then(t=>document.querySelector('main').innerHTML=t)"
                if self.quick_retry:
                    click += ".catch(()=>setTimeout(()=>fetch('/applicant/vacancy_response/popup',{method:'POST',body:f}),200))"
            body = f'''<h1>Вакансия {job_id}</h1><div data-qa="vacancy-description">Разработка Python API.</div>
            <a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId={job_id}" onclick="{click}">Откликнуться</a>
            <button data-qa="vacancy-body-mark-favorite_{favorite}"
            onclick="fetch('/favorite?id={job_id}').then(()=>this.setAttribute('data-qa','vacancy-body-mark-favorite_true'))">Избранное</button>'''
            if applied:
                body += "<p>Вы&nbsp;откликнулись</p>"
                if self.ignore_letter:
                    body += '<button data-qa="responded-success-attach-cover-letter">Приложить сопроводительное письмо</button>'
        elif path == "/favorite":
            self.favorites.add(query["id"][0])
        elif path == "/applicant/vacancy_response/popup" and route.request.method == "GET":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body='<div role="dialog">' + self.response_form(query["vacancyId"][0]) + '</div>')
            return
        elif path == "/applicant/vacancy_response":
            raise AssertionError("Опасный прямой переход к отклику не должен выполняться")
        elif path == "/applicant/vacancy_response/popup":
            fields = read_fields(route.request.headers.get("content-type", ""), route.request.post_data_buffer)
            if self.text_captcha_required:
                code = fields.get("captchaText", [""])[0]
                self.captcha_attempts.append(code)
                if code != "Ab42":
                    form = self.response_form(fields["vacancy_id"][0])
                    extra = '''<input type="hidden" name="resume_hash" value="__RESUME__">
                    <img data-qa="captcha-image" width="180" height="60" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1sAAAAASUVORK5CYII=">
                    <input name="captchaText">'''.replace("__RESUME__", RESUME_ID)
                    form = form.replace("<textarea", extra + "<textarea", 1)
                    route.fulfill(status=403, content_type="text/html; charset=utf-8", body=form)
                    return
            self.submissions.append({"id": fields["vacancy_id"][0], "resume_id": fields["resume_hash"][0],
                                     "letter": "" if self.ignore_letter else fields.get("letter", [""])[0]})
            with closing(sqlite3.connect(self.root / "output/state.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT status FROM vacancies WHERE id=?", (fields["vacancy_id"][0],)).fetchone()[0], "submission_pending")
            body = "Отклик отправлен" if self.confirm_submission else "Ожидание"
            if self.captcha_after_submit:
                body = '<input name="captchaText" aria-label="CAPTCHA">'
        else:
            raise AssertionError("Unexpected URL: " + route.request.url)
        route.fulfill(status=200, content_type="text/html; charset=utf-8",
                      body="<!doctype html><html><body><main>" + body + "</main></body></html>")

    def response_form(self, job_id):
        extra = ""
        if job_id == "2":
            extra = "<p>Для отклика необходимо ответить на несколько вопросов работодателя</p>"
        if job_id == "3":
            extra = '<input name="question_1" type="text" required aria-label="Вопрос работодателя">'
        body = '''__EXTRA__
        <form name="vacancy_response" onsubmit="event.preventDefault();
          fetch('/applicant/vacancy_response/popup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vacancy_id:'__ID__',resume_hash:document.querySelector('[name=resume_hash]')?.value || window.chosenResume,
          captchaText:document.querySelector('[name=captchaText]')?.value || '',letter:document.querySelector('textarea').value})}).then(r=>r.text()).then(t=>document.querySelector('main').innerHTML=t)">
        <button type="button" data-qa="resume-title" onclick="document.getElementById('choices').hidden=false">ML-инженер</button>
        <div data-qa="vacancy-response-letter-toggle" onclick="document.querySelector('textarea').style.display='block'">Добавить письмо</div>
        <textarea style="display:none" data-qa="vacancy-response-popup-form-letter-input"></textarea>
        <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></form>
        <div id="choices" hidden><input type="radio" value="__RESUME__"
          onchange="window.chosenResume=this.value; document.querySelector('[data-qa=resume-title]').textContent='__TITLE__'; document.getElementById('choices').hidden=true"></div>'''
        # В третьем случае поле работодателя находится непосредственно в форме.
        if job_id == "3":
            body = body.replace("__EXTRA__", "").replace("<textarea", extra + "<textarea", 1)
        return body.replace("__EXTRA__", extra).replace("__ID__", job_id).replace("__RESUME__", RESUME_ID).replace("__TITLE__", RESUME_TITLE)

    def run_task(self, task="apply", dry_run=False, **kwargs):
        return actions.run_actions(self.page, task=task, config_path=self.config_path, dry_run=dry_run, **kwargs)

    def test_bump_all_uses_full_list_and_avoids_paid_buttons(self):
        self.run_task("bump")
        self.assertEqual(self.bumps, ["bumped"])
        self.assertEqual(self.paid_clicks, 0)
        inventory = json.loads((self.root / "output/resumes.json").read_text(encoding="utf-8"))
        self.assertEqual(len(inventory), 3)

    def test_already_bumped_resumes_schedule_the_earliest_site_timer(self):
        def list_resumes(route):
            route.fulfill(content_type="text/html; charset=utf-8", body='''
                <div data-qa="resume"><div data-qa="resume-title">Первое</div>
                <div>Поднять вручную можно через 45 минут</div><button>Поднять автоматически</button></div>
                <div data-qa="resume"><div data-qa="resume-title">Второе</div>
                <div>Поднять вручную можно через 2 часа</div><button>Поднять автоматически</button></div>
                <div data-qa="resume"><div data-qa="resume-title">Скрытое</div></div>''')

        self.context.route("**/applicant/my_resumes", list_resumes)
        delay = self.run_task("bump")
        self.assertGreater(delay, 2680)
        self.assertLessEqual(delay, 2700)
        self.assertEqual(self.bumps, [])
        self.assertEqual(self.paid_clicks, 0)

    def test_scheduled_bump_uses_separate_tab_and_keeps_main_form_and_storage(self):
        import service
        from runtime_control import RunControl, RunStopped
        from response_guard import ResponseGuard

        now = [0]
        control = RunControl(clock=lambda: now[0])
        self.addCleanup(control.stop)
        original_checkpoint = control.checkpoint
        bump_pages = []

        def list_resumes(route):
            route.fulfill(content_type="text/html; charset=utf-8", body='''
                <div data-qa="resume"><div data-qa="resume-title">Python Backend</div>
                <button onclick="fetch('/bump').then(()=>this.outerHTML='Поднять вручную можно через 4 часа')">Поднять в поиске</button>
                <button>Поднять автоматически</button></div>''')

        self.context.route("**/applicant/my_resumes", list_resumes)

        def checkpoint(deadline=None):
            if now[0] >= 100:
                raise RunStopped()
            original_checkpoint(deadline)

        def wait_until(target, *, deadline=None):
            while now[0] < target:
                checkpoint(deadline)
                now[0] = min(target, now[0] + 1)
            checkpoint(deadline)

        real_actions = actions.run_actions

        def run(page, **kwargs):
            if kwargs["task"] == "bump":
                bump_pages.append(page)
                if len(bump_pages) == 1:
                    return 5  # Резюме поднимали до запуска; осталось 5 секунд.
                return real_actions(page, **kwargs)
            page.goto("http://hh.local/vacancy/1")
            page.set_content('<form><input name="draft" value="kept"></form>')
            page.evaluate("sessionStorage.setItem('draft','kept')")
            with ResponseGuard(page, "http://hh.local", "1", RESUME_ID, LETTER, dry_run=False,
                               reserve=lambda: True, checkpoint=control.checkpoint) as guard:
                guard.active = True
                control.wait_until(8)
                self.assertEqual(len(bump_pages), 1)
            control.checkpoint()
            self.assertEqual(len(bump_pages), 2)
            self.assertEqual(page.url, "http://hh.local/vacancy/1")
            self.assertEqual(page.locator('[name="draft"]').input_value(), "kept")
            self.assertEqual(page.evaluate("sessionStorage.getItem('draft')"), "kept")
            kwargs["progress"].checked = kwargs["progress"].max_vacancies
            return 1

        self.config["cycle_hours"] = 100 / 3600
        self.write_config()
        with (patch.object(control, "checkpoint", side_effect=checkpoint),
              patch.object(control, "wait_until", side_effect=wait_until),
              patch.object(service, "run_actions", side_effect=run)):
            service.run_continuous(self.page, task="all", config_path=self.config_path,
                                   dry_run=False, control=control)
        self.assertEqual(len(bump_pages), 2)
        self.assertIs(bump_pages[0], self.page)
        self.assertIsNot(bump_pages[1], self.page)
        self.assertIs(bump_pages[1].context, self.page.context)
        self.assertEqual(self.bumps, ["bumped"])
        self.assertFalse(self.page.is_closed())

    def test_model_plans_search_from_resume_before_collecting_vacancies(self):
        self.config["search_mode"] = "model"
        self.write_config()
        plan = {"query": "python backend", "search_field": "name",
                "experience": ["noExperience", "between1And3"], "level": "middle",
                "maximum_experience": "between1And3", "reason": "По подтверждённому опыту.", "cached": False}
        with patch.object(actions, "plan_search", return_value=plan) as planner:
            self.run_task("search")
        self.assertIn("Python", planner.call_args.args[1])
        self.assertNotIn("PRIVATE_CONTACT", planner.call_args.args[1])
        self.assertEqual(planner.call_args.kwargs, {"location": "moscow_any", "include_lower": True,
                                                  "previous_queries": [], "round_number": 1})
        saved = json.loads((self.root / "output/search-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, plan)
        self.assertEqual(parse_qs(urlparse(self.page.url).query)["experience"], [plan["experience"][-1]])
        self.model.assert_not_called()
        self.assertEqual(self.submissions, [])

    def test_next_search_receives_persisted_previous_query_and_new_round(self):
        self.config["search_mode"] = "model"
        self.write_config()
        plan = {"query": "python backend", "search_field": "name", "experience": ["between1And3"],
                "level": "middle", "reason": "По резюме.", "cached": False}
        with patch.object(actions, "plan_search", return_value=plan) as planner:
            self.run_task("search")
            self.run_task("search")
        self.assertEqual(planner.call_args_list[0].kwargs["previous_queries"], [])
        self.assertEqual(planner.call_args_list[1].kwargs["previous_queries"], [plan["query"]])
        self.assertGreater(planner.call_args_list[1].kwargs["round_number"],
                           planner.call_args_list[0].kwargs["round_number"])

    def test_review_pacing_and_quota_continue_across_search_passes(self):
        self.config["max_vacancies"] = 4
        self.write_config()
        control = FastControl()
        progress = CycleProgress(4, 4)
        self.search_pages = {0: [1]}
        self.run_task(control=control, progress=progress, cycle_started=0, deadline=40)
        self.assertEqual(progress.checked, 1)
        self.search_pages = {0: [1, 2]}
        self.run_task(control=control, progress=progress, cycle_started=0, deadline=40)
        self.assertEqual(progress.checked, 2)
        self.assertIn(10, control.waits)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(progress.sent, 1)
        self.assertEqual(progress.seen_ids, {"1", "2"})

    def test_pass_with_exhausted_shared_quota_does_not_plan_or_evaluate(self):
        self.config["search_mode"] = "model"
        self.write_config()
        with patch.object(actions, "plan_search") as planner:
            self.assertEqual(self.run_task(progress=CycleProgress(400, 400, checked=400)), 0)
        planner.assert_not_called()
        self.model.assert_not_called()

    def test_seen_vacancies_are_skipped_across_dry_run_searches_without_history_writes(self):
        self.model.return_value = dict(self.model.return_value, suitable=False, score=20)
        progress = CycleProgress(10, 10)
        self.search_pages = {0: [1]}
        self.run_task(dry_run=True, progress=progress)
        self.search_pages = {0: [1, 2]}
        self.run_task(dry_run=True, progress=progress)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(progress.checked, 2)
        self.assertEqual(progress.seen_ids, {"1", "2"})
        with closing(sqlite3.connect(self.root / "output/state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)

    def test_api_budget_stop_ends_batch_without_submitting_or_processing_next_vacancy(self):
        from openai_client import OpenAIStop

        self.model.side_effect = OpenAIStop("Локальный бюджет OpenAI исчерпан.")
        with self.assertRaises(OpenAIStop):
            self.run_task()
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self.submissions, [])

    def test_search_visits_own_and_lower_experience_separately_and_deduplicates(self):
        self.config["max_vacancies"] = 10
        self.config["search_filters"] = {"experience": ["noExperience", "between1And3"]}
        self.write_config()
        visited = []

        def search(route):
            experience = parse_qs(urlparse(route.request.url).query)["experience"]
            self.assertEqual(len(experience), 1)
            visited.extend(experience)
            ids = [1, 2] if experience == ["between1And3"] else [2, 3]
            route.fulfill(content_type="text/html; charset=utf-8", body="".join(
                f'<a data-qa="serp-item__title" href="/vacancy/{value}">Вакансия {value}</a>' for value in ids))

        self.context.route("**/search/vacancy?**", search)
        self.run_task("search")
        self.assertEqual(visited, ["between1And3", "noExperience"])
        saved = json.loads((self.root / "output/vacancies.json").read_text(encoding="utf-8"))
        self.assertEqual([vacancy["id"] for vacancy in saved], ["1", "2", "3"])

    def test_real_empty_hh_message_with_nonbreaking_spaces_continues_other_experience_band(self):
        self.config["max_vacancies"] = 10
        self.config["search_filters"] = {"experience": ["noExperience", "between1And3"]}
        self.write_config()

        def search(route):
            experience = parse_qs(urlparse(route.request.url).query)["experience"]
            if experience == ["between1And3"]:
                body = '<h1>По\xa0запросу «python backend» ничего не\xa0найдено</h1><h2>Ничего не\xa0нашлось</h2>'
            else:
                body = '<a data-qa="serp-item__title" href="/vacancy/123">python junior backend</a>'
            route.fulfill(content_type="text/html; charset=utf-8", body=body)

        self.context.route("**/search/vacancy?**", search)
        self.run_task("search")
        saved = json.loads((self.root / "output/vacancies.json").read_text(encoding="utf-8"))
        self.assertEqual([vacancy["id"] for vacancy in saved], ["123"])
        events = [json.loads(line)["event"] for line in (self.root / "output/events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("search_empty", events)
        self.model.assert_not_called()

    def test_async_empty_search_is_not_a_card_layout_error(self):
        self.context.route("**/search/vacancy?**", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body='''<h2 id="result">Загрузка</h2>
            <script>setTimeout(()=>document.querySelector('#result').textContent='Ничего не\\u00a0нашлось',100)</script>'''))
        self.run_task("search")
        self.assertEqual(json.loads((self.root / "output/vacancies.json").read_text(encoding="utf-8")), [])
        self.assertEqual(self.submissions, [])
        self.model.assert_not_called()

    def test_unknown_search_page_is_not_silently_treated_as_empty(self):
        self.context.route("**/search/vacancy?**", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body='<h1>Неизвестная разметка поиска</h1>'))
        with self.assertRaisesRegex(RuntimeError, "Не удалось распознать выдачу"):
            self.run_task("search")
        diagnostic = json.loads((self.root / "output/search-error.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["headings"], ["Неизвестная разметка поиска"])
        self.assertEqual(self.submissions, [])

    def test_application_resume_letter_questionnaires_and_duplicate_protection(self):
        with self.assertLogs("hh_browser", level="INFO") as output:
            self.run_task()
        self.assertEqual(self.submissions, [{"id": "1", "resume_id": RESUME_ID, "letter": LETTER}])
        self.assertEqual(self.favorites, {"2", "3"})
        self.assertEqual(self.model.call_count, 3)
        self.assertNotIn("PRIVATE_CONTACT", self.model.call_args.args[2])
        console = "\n".join(output.output)
        self.assertIn("Открыл вакансию", console)
        self.assertIn("Оценка: 85/100", console)
        self.assertIn("Начал общий поиск", console)
        self.assertIn("area=1", console)
        self.assertIn("в очереди всего 3; по локальной истории пропущено всего 0", console)
        self.assertIn("Отправил отклик", console)
        self.assertIn(LETTER, console)
        self.run_task()
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.model.call_count, 3)

    def test_dry_run_makes_no_external_changes_and_does_not_block_later_apply(self):
        self.run_task("all", dry_run=True)
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.favorites, set())
        self.assertEqual(self.bumps, [])
        self.assertTrue((self.root / "output/letters/1.txt").exists())
        self.run_task()
        self.assertEqual(len(self.submissions), 1)

    def test_quick_response_includes_letter_before_sending(self):
        self.quick_response = True
        self.config["max_vacancies"] = 1
        self.write_config()
        self.run_task()
        self.assertEqual(self.submissions, [{"id": "1", "resume_id": RESUME_ID, "letter": LETTER}])
        self.run_task()
        self.assertEqual(len(self.submissions), 1)

    def recover_page(self, challenge):
        self.page.close()
        self.page = self.context.new_page()
        self.page.set_default_timeout(3000)
        return self.page

    def test_captcha_retries_current_vacancy_with_new_page(self):
        original_navigate = actions.navigate
        challenges = []

        def challenge_once(page, url):
            if url.endswith("/vacancy/1") and not challenges:
                challenges.append(url)
                raise HumanRequired("CAPTCHA", url=url)
            return original_navigate(page, url)

        with patch.object(actions, "navigate", side_effect=challenge_once):
            self.run_task(on_human_required=self.recover_page)
        self.assertEqual(len(challenges), 1)
        self.assertEqual(self.submissions, [{"id": "1", "resume_id": RESUME_ID, "letter": LETTER}])
        self.assertEqual(self.favorites, {"2", "3"})

    def test_telegram_code_submits_same_form_with_letter_after_rejection(self):
        from unittest.mock import Mock
        from runtime_control import RunControl

        self.quick_response = self.text_captcha_required = True
        self.config["max_vacancies"] = 1
        self.write_config()
        client = Mock()
        client.request_answer.side_effect = ["wrong", "Ab42"]
        page = self.page
        relay = CaptchaRelay(client, RunControl(), verification_timeout=0.1)
        with captcha_handler(relay.solve):
            self.run_task()
        self.assertIs(self.page, page)
        self.assertFalse(page.is_closed())
        self.assertEqual(self.captcha_attempts, ["", "wrong", "Ab42"])
        self.assertEqual(self.submissions, [{"id": "1", "resume_id": RESUME_ID, "letter": LETTER}])
        self.assertEqual(client.request_answer.call_count, 2)

    def test_manual_browser_captcha_keeps_letter_and_sends_one_application(self):
        import threading
        from unittest.mock import Mock
        from runtime_control import RunControl
        from telegram_captcha import TelegramError

        self.quick_response = self.text_captcha_required = True
        self.config["max_vacancies"] = 1
        self.write_config()
        client, control = Mock(), RunControl()
        waiting, stopped = threading.Event(), threading.Event()

        def wait_for_answer(_picture, *, cancelled):
            waiting.set()
            while not cancelled():
                stopped.wait(0.01)
            stopped.set()
            raise TelegramError("Ожидание Telegram остановлено.")

        client.request_answer.side_effect = wait_for_answer
        original_check = control.check_stopped
        submitted = False

        def manual_check():
            nonlocal submitted
            original_check()
            if waiting.is_set() and not submitted:
                submitted = True
                self.page.locator('[name="captchaText"]').fill("Ab42")
                self.page.locator('[data-qa="vacancy-response-submit-popup"]').click()

        relay = CaptchaRelay(client, control, verification_timeout=0.1)
        with patch.object(control, "check_stopped", side_effect=manual_check), captcha_handler(relay.solve):
            self.run_task()
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.captcha_attempts, ["", "Ab42"])
        self.assertEqual(self.submissions, [{"id": "1", "resume_id": RESUME_ID, "letter": LETTER}])
        self.assertFalse(self.page.is_closed())

    def test_captcha_after_submit_keeps_history_and_does_not_resubmit(self):
        self.captcha_after_submit = True
        challenges = []

        def recover(challenge):
            challenges.append(challenge.url)
            return self.recover_page(challenge)

        self.run_task(on_human_required=recover)
        self.assertEqual(challenges, ["http://hh.local/vacancy/1"])
        self.assertEqual(len(self.submissions), 1)
        with closing(sqlite3.connect(self.root / "output/state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM vacancies WHERE id='1'").fetchone()[0], "submission_unknown")

    def test_quick_dry_run_and_wrong_resume_cannot_submit(self):
        self.quick_response = True
        self.config["max_vacancies"] = 1
        self.write_config()
        self.run_task(dry_run=True)
        self.assertEqual(self.submissions, [])
        self.quick_resume = OTHER_ID
        self.run_task()
        self.assertEqual(self.submissions, [])

    def test_existing_hh_response_with_nonbreaking_space_is_not_touched(self):
        self.config["max_vacancies"] = 1
        self.write_config()
        self.submissions.append({"id": "1", "resume_id": RESUME_ID, "letter": ""})
        self.run_task()
        self.assertEqual(len(self.submissions), 1)
        self.model.assert_not_called()

    def test_quick_dry_run_cancels_delayed_site_retry(self):
        self.quick_response = self.quick_retry = True
        self.config["max_vacancies"] = 1
        self.write_config()
        self.run_task(dry_run=True)
        self.page.wait_for_timeout(500)
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.page.url, "about:blank")

    def test_missing_server_letter_confirmation_stops_batch(self):
        self.quick_response = True
        self.ignore_letter = True
        with self.assertRaises(actions.LetterDeliveryError):
            self.run_task()
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.model.call_count, 1)

    def test_unknown_result_is_not_submitted_again(self):
        self.confirm_submission = False
        self.run_task()
        with closing(sqlite3.connect(self.root / "output/state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM vacancies WHERE id='1'").fetchone()[0], "submission_unknown")
        self.run_task()
        self.assertEqual(len(self.submissions), 1)

    def test_scores_below_threshold_are_cached_and_config_change_rechecks(self):
        self.model.return_value = dict(self.model.return_value, score=69)
        self.run_task()
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.favorites, set())
        self.assertEqual(self.model.call_count, 3)
        self.run_task()
        self.assertEqual(self.model.call_count, 3)
        self.config["minimum_score"] = 60
        self.write_config()
        self.run_task()
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.model.call_count, 6)

    def test_cycle_paces_vacancies_and_does_not_submit_after_deadline(self):
        control = FastControl()
        self.run_task(control=control, cycle_started=0, deadline=12)
        self.assertEqual(control.waits, [4, 8])
        self.assertEqual(len(self.submissions), 1)

    def test_model_finishing_after_deadline_cannot_submit(self):
        control = FastControl()
        result = self.model.return_value

        def late_model(*args):
            control.current = 12
            return result

        self.model.side_effect = late_model
        with self.assertRaises(CycleExpired):
            self.run_task(control=control, cycle_started=0, deadline=12)
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.favorites, set())

    def test_search_caps_at_400_and_finds_next_jobs_past_history(self):
        self.config["max_vacancies"] = 400
        self.write_config()
        self.search_pages = {0: list(range(1, 301)), 1: list(range(301, 601))}
        self.run_task("search")
        output_path = self.root / "output/vacancies.json"
        vacancies = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(len(vacancies), 400)
        self.assertEqual(vacancies[-1]["id"], "400")
        journal = Journal(self.root / "output")
        try:
            journal.db.executemany("INSERT INTO vacancies VALUES (?, 'applied')", [(str(i),) for i in range(1, 401)])
            journal.db.commit()
        finally:
            journal.close()
        self.run_task("search")
        vacancies = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(len(vacancies), 200)
        self.assertEqual(vacancies[0]["id"], "401")
        self.assertEqual(self.submissions, [])

    def test_missing_letter_format_stops_before_applications(self):
        self.config["letter_instructions_file"] = None
        self.write_config()
        with self.assertRaisesRegex(RuntimeError, "Формат сопроводительного"):
            self.run_task()
        self.assertEqual(self.submissions, [])
        self.model.assert_not_called()

    def test_missing_employer_answer_prevents_high_score_submission(self):
        self.config["max_vacancies"] = 1
        self.write_config()
        self.model.return_value = dict(self.model.return_value, score=99,
                                       missing_requirements=["Не указаны зарплатные ожидания"])
        with self.assertLogs("hh_browser", level="INFO") as output:
            self.run_task()
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.favorites, set())
        self.assertIn("Не указаны зарплатные ожидания", "\n".join(output.output))


class OpenAIAdapterTests(unittest.TestCase):
    def test_valid_structured_letter_and_request(self):
        with patch("letters.request_json", return_value={"data": dict(MODEL_PARTS)}) as request:
            result = generate_letter({"model": "test"}, "Кратко", MODEL_RESUME, "Вакансия")
        self.assertIn("suitable", request.call_args.args[1]["required"])
        self.assertEqual(request.call_args.args[3]["resume"], MODEL_RESUME)
        self.assertEqual(request.call_args.kwargs["purpose"], "vacancy_review")
        self.assertEqual(result["letter"], LETTER)

    def ask_with_result(self, result, vacancy="Вакансия"):
        with patch("letters.request_json", return_value={"data": result}):
            return generate_letter({"model": "test"}, "Формат", MODEL_RESUME, vacancy)

    def test_required_phrase_and_missing_answers_are_verified(self):
        result = dict(MODEL_PARTS)
        response = self.ask_with_result(result, 'В сопроводительном письме укажите «Синий кактус».')
        self.assertTrue(response["missing_requirements"])
        result.update(requirements=[{
            "instruction": "Укажите кодовую фразу", "answer": "Синий кактус", "fulfilled": True,
        }])
        self.assertEqual(self.ask_with_result(result, 'В сопроводительном письме укажите «Синий кактус».')["missing_requirements"], [])
        result["requirements"][0]["answer"] = "Несуществующий ответ"
        self.assertTrue(self.ask_with_result(result, 'В сопроводительном письме укажите «Синий кактус».')["missing_requirements"])

    def test_absence_of_requirements_is_not_an_unanswered_question(self):
        for instruction in (
            "Нет специальных требований к сопроводительному письму.",
            "Нет дополнительных требований к письму",
            "Специальные требования к сопроводительному письму отсутствуют.",
            "Требования к сопроводительному письму не указаны",
        ):
            for fulfilled in (False, True):
                with self.subTest(instruction=instruction, fulfilled=fulfilled):
                    result = self.ask_with_result(dict(MODEL_PARTS, requirements=[{
                        "instruction": instruction, "answer": "", "fulfilled": fulfilled,
                    }]))
                    self.assertEqual(result["requirements"], [])
                    self.assertEqual(result["missing_requirements"], [])
                    self.assertEqual(result["letter"], LETTER)

    def test_absence_note_cannot_hide_actual_missing_answers(self):
        no_requirements = {"instruction": "Нет специальных требований к сопроводительному письму.",
                           "answer": "", "fulfilled": True}
        salary = {"instruction": "Укажите зарплатные ожидания, они не указаны в резюме.",
                  "answer": "", "fulfilled": False}
        result = self.ask_with_result(dict(MODEL_PARTS, requirements=[no_requirements, salary]))
        self.assertEqual(result["missing_requirements"], [salary["instruction"]])
        result = self.ask_with_result(dict(MODEL_PARTS, requirements=[no_requirements]),
                                      'В сопроводительном письме укажите «Синий кактус».')
        self.assertEqual(result["missing_requirements"], ["Дословная фраза: Синий кактус"])

    def test_fixed_structure_unlisted_stack_and_invalid_scores(self):
        result = self.ask_with_result(dict(MODEL_PARTS, letter="Привет! Я Python. Пока!"))
        self.assertEqual(result["letter"], LETTER)
        for changes in ({"stack": ["Kubernetes"]}, {"interest": ""}, {"score": 101}, {"score": True}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.ask_with_result(dict(MODEL_PARTS, **changes))

    def test_employer_cannot_silently_override_fixed_greeting(self):
        result = dict(MODEL_PARTS, requirements=[{
            "instruction": "Начните письмо с кодовой фразы", "answer": "Синий кактус", "fulfilled": True,
        }])
        response = self.ask_with_result(result, 'Начните сопроводительное письмо со слов «Синий кактус».')
        self.assertTrue(response["letter"].startswith("Здравствуйте!"))
        self.assertTrue(any("конфликтует" in item for item in response["missing_requirements"]))

    def test_invalid_model_output_is_rejected(self):
        for content in ('not json', {"suitable": "true", "reason": "ok", "letter": "text"},
                        {"suitable": True, "reason": "ok", "letter": ""}):
            with self.subTest(content=content), patch("letters.request_json", return_value={"data": content}):
                with self.assertRaises(RuntimeError):
                    generate_letter({"model": "test"}, "Стиль", "Резюме", "Вакансия")

    def test_api_failure_does_not_produce_a_letter(self):
        with patch("letters.request_json", side_effect=RuntimeError("API недоступно")) as request:
            with self.assertRaises(RuntimeError):
                generate_letter({"model": "test"}, "Стиль", "Резюме", "Вакансия")
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
