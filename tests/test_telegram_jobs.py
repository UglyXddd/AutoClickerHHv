import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import telegram_jobs as jobs
from openai_client import OpenAIError


def card(post_id, day, text):
    return f'''<div class="tgme_widget_message_wrap"><div data-post="{post_id}">
        <div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>
        <div class="tgme_widget_message_footer"><a href="https://t.me/{post_id}">
            <time datetime="2026-11-{day:02}T12:00:00+00:00">12:00</time></a></div>
        </div></div>'''


class TelegramJobTests(unittest.TestCase):
    def test_public_preview_pages_back_to_cutoff_and_uses_only_post_contacts(self):
        recent = card("p_rabota/30", 8, 'Junior Python<br>REST API · FastAPI<br>'
                      '<a href="https://t.me/team_hiring">Откликнуться</a> или jobs@example.com, +7 (999) 123-45-67<br>'
                      '<a href="https://t.me/p_rabota">Подписка</a> @hr_rus')
        boundary = card("p_rabota/20", 6, "Middle Python")
        old = card("p_rabota/19", 2, "Python internship")
        fetch = Mock(side_effect=[recent + boundary, old])
        cutoff = datetime(2026, 11, 5, tzinfo=timezone.utc)
        posts = list(jobs.read_channel("p_rabota", cutoff, fetch=fetch))
        self.assertEqual([post.key for post in posts], ["p_rabota/30", "p_rabota/20"])
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         ["https://t.me/s/p_rabota", "https://t.me/s/p_rabota?before=20"])
        self.assertIn("Python\nREST API", posts[0].text)
        self.assertEqual(jobs.contact_options(posts[0]), [
            {"value": "https://t.me/team_hiring", "label": "Откликнуться"},
            {"value": "jobs@example.com", "label": ""},
            {"value": "+79991234567", "label": ""},
        ])

    def test_scan_writes_message_for_match_and_keeps_report_after_error(self):
        post = jobs.ChannelPreview(card("job_python/11", 8, 'Python-разработчик. '
                                   '<a href="https://t.me/team_hiring">Откликнуться</a>')).posts[0]
        other = jobs.ChannelPreview(card("job_python/12", 9, "Python Backend, отклик hr@example.com")).posts[0]
        body = "Пишу по вакансии Python-разработчика. Мой стек — Python и FastAPI."
        resume = "Python, FastAPI. Опыт работы в компании «Щёлково Агрохим»."
        match = {"title": "Python-разработчик", "company": "Тест", "score": 60,
                 "reason": "Совпадает опыт FastAPI.", "contact_indices": [0], "residence_requested": False,
                 "message_body": body + " Живу в г. Щёлково.",
                 "missing_details": "Ожидания по зарплате"}
        options = [(post, jobs.contact_options(post)), (other, jobs.contact_options(other))]
        response = {"data": {"matches": [dict(match, score=59), dict(match, contact_indices=[8]), match]}}
        with TemporaryDirectory() as temporary:
            config = {"minimum_score": 60, "search_location": "moscow_any", "telegram_candidate_city": "Москва",
                      "openai": {}, "output": Path(temporary)}
            with (patch.object(jobs, "collect_posts", return_value=(options, False)),
                  patch.object(jobs, "load_api_key"),
                  patch.object(jobs, "enable_terminal_colors", return_value=True),
                  patch.object(jobs, "request_json", side_effect=[response, OpenAIError("тайм-аут")]) as api,
                  self.assertLogs(jobs.LOG, level="INFO") as logs,
                  redirect_stdout(io.StringIO()) as console):
                self.assertEqual(jobs.run_scan(config, resume, 30, prompt_key=False), 1)
            report = (Path(temporary) / jobs.REPORT).read_text(encoding="utf-8")
        self.assertEqual(report, "https://t.me/team_hiring\nПост: https://t.me/job_python/11\n\nЗдравствуйте!\n\n"
                         f"{body}\n\n[Допишите перед отправкой: Ожидания по зарплате]\n\n"
                         f"Прикрепляю резюме. Буду рад обсудить детали!\n{jobs.SEPARATOR}\n{jobs.SEPARATOR}\n\n")
        self.assertIn("https://t.me/job_python/11", console.getvalue())
        self.assertIn(jobs.RESULT_COLOR + jobs.SEPARATOR, console.getvalue())
        self.assertIn(jobs.SEPARATOR + jobs.RESET, console.getvalue())
        self.assertIn("Проверка неполная", logs.output[-1])
        self.assertEqual(api.call_args_list[0].args[3]["resume"], resume)
        self.assertEqual(api.call_args_list[0].args[3]["candidate_city"], "Москва")

    def test_requested_residence_comes_from_settings_not_from_employer_city(self):
        item = {"title": "Python-разработчик", "message_body": "В компании «Щёлково Агрохим» работал над API. Я в Щёлково.",
                "residence_requested": True, "missing_details": ""}
        draft = jobs.format_draft(item, "Москва")
        self.assertIn("Город проживания — Москва.", draft)
        self.assertIn("В компании «Щёлково Агрохим» работал над API.", draft)
        self.assertNotIn("Я в Щёлково.", draft)
        self.assertIn("[Укажите город проживания.]", jobs.format_draft(item, None))

    def test_uses_only_a_snapshot_of_the_selected_resume(self):
        with TemporaryDirectory() as temporary:
            config = {"output": Path(temporary), "resume_id": "wanted"}
            snapshot = config["output"] / jobs.RESUME_SNAPSHOT
            snapshot.write_text(json.dumps({"resume_id": "other", "text": "Старое резюме"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Сначала запустите"):
                jobs.resume_for_scan(config)
            snapshot.write_text(json.dumps({"resume_id": "wanted", "text": "Python, FastAPI"}), encoding="utf-8")
            self.assertEqual(jobs.resume_for_scan(config), "Python, FastAPI")

    def test_rate_limit_stops_collection_in_other_channels(self):
        error = HTTPError("https://t.me/s/p_rabota", 429, "Too many requests", {}, None)
        with patch.object(jobs, "read_channel", side_effect=error) as read:
            self.assertEqual(jobs.collect_posts(datetime(2026, 11, 1, tzinfo=timezone.utc)), ([], True))
        read.assert_called_once()
