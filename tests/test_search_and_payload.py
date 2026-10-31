import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

from actions import search_url
from response_guard import ResponseGuard, add_letter, read_fields


class SearchTests(unittest.TestCase):
    def test_moscow_any_is_global_search_without_work_format(self):
        query = parse_qs(urlparse(search_url({"search_query": "python backend", "search_location": "moscow_any",
            "search_filters": {"resume": "old", "from": "resumelist", "work_format": "REMOTE", "items_on_page": 50}})).query)
        self.assertEqual(query["area"], ["1"])
        self.assertEqual(query["search_field"], ["name"])
        self.assertEqual(query["text"], ["python backend"])
        self.assertEqual(query["items_on_page"], ["20"])
        self.assertNotIn("resume", query)
        self.assertNotIn("work_format", query)

    def test_russia_remote_filters_are_preserved_on_url(self):
        query = parse_qs(urlparse(search_url({"search_query": "Python", "search_location": "russia_remote"})).query)
        self.assertEqual(query["area"], ["113"])
        self.assertEqual(query["work_format"], ["REMOTE"])


class PayloadTests(unittest.TestCase):
    def test_unknown_submission_result_never_allows_captcha_retry(self):
        guard = ResponseGuard(Mock(), "https://hh.ru", "123", "resume", "Письмо", dry_run=False,
                              reserve=Mock(), checkpoint=lambda: None)
        guard.sent = guard.reserved = True
        guard.response = Mock(status=200)
        guard.response.json.return_value = {"ok": True}
        with self.assertRaisesRegex(RuntimeError, "повтор заблокирован"):
            guard.prepare_captcha_retry()
        self.assertTrue(guard.sent)

    def test_explicit_captcha_rejection_reuses_one_reservation(self):
        guard = ResponseGuard(Mock(), "https://hh.ru", "123", "resume", "Письмо", dry_run=False,
                              reserve=Mock(), checkpoint=lambda: None)
        guard.sent = guard.reserved = True
        guard.response = Mock(status=403)
        guard.prepare_captcha_retry()
        self.assertFalse(guard.sent)
        self.assertTrue(guard.reserved)
        guard.reserve.assert_not_called()

    def test_manual_captcha_cannot_repeat_success_or_change_resume_or_letter(self):
        for status, resume, letter in ((200, "resume", "Письмо"),
                                        (403, "another-resume", "Письмо"),
                                        (403, "resume", "Другое письмо")):
            with self.subTest(status=status, resume=resume, letter=letter):
                guard = ResponseGuard(Mock(), "https://hh.ru", "123", "resume", "Письмо", dry_run=False,
                                      reserve=Mock(), checkpoint=lambda: None)
                guard.sent = guard.reserved = guard.active = guard.captcha_submission = True
                guard.response = Mock(status=status)
                guard.response.json.return_value = {"ok": True}
                route = Mock()
                route.request.url = "https://hh.ru/applicant/vacancy_response/popup"
                route.request.method = "POST"
                route.request.headers = {"content-type": "application/x-www-form-urlencoded"}
                from urllib.parse import urlencode
                route.request.post_data_buffer = urlencode({"vacancy_id": "123", "resume_hash": resume,
                                                             "letter": letter, "captchaText": "Ab42"}).encode()
                guard._route(route)
                route.abort.assert_called_once_with()
                route.fallback.assert_not_called()
                guard.reserve.assert_not_called()

    def test_multipart_letter_keeps_resume_and_other_browser_fields(self):
        content_type = 'multipart/form-data; boundary=Boundary123'
        body = (b'--Boundary123\r\nContent-Disposition: form-data; name="resume_hash"\r\n\r\nresume123\r\n'
                b'--Boundary123\r\nContent-Disposition: form-data; name="_xsrf"\r\n\r\nkeep-token\r\n--Boundary123--\r\n')
        letter = "Здравствуйте!\n\nМой стек — Python.\n\nБуду рад обсудить детали."
        result = read_fields(content_type, add_letter(content_type, body, letter))
        self.assertEqual(result, {"resume_hash": ["resume123"], "_xsrf": ["keep-token"], "letter": [letter]})

    def test_json_and_form_letter_encode_unicode(self):
        for content_type, body in (("application/json", b'{"vacancy_id":"123"}'),
                                    ("application/x-www-form-urlencoded", b'vacancy_id=123')):
            with self.subTest(content_type=content_type):
                result = read_fields(content_type, add_letter(content_type, body, "Здравствуйте!"))
                self.assertEqual(result["vacancy_id"], ["123"])
                self.assertEqual(result["letter"], ["Здравствуйте!"])

    def test_empty_multipart_letter_is_replaced_not_duplicated(self):
        body = b'--X\r\nContent-Disposition: form-data; name="letter"\r\n\r\n\r\n--X\r\nContent-Disposition: form-data; name="captchaText"\r\n\r\nAb42\r\n--X--\r\n'
        content_type = "multipart/form-data; boundary=X"
        fields = read_fields(content_type, add_letter(content_type, body, "Здравствуйте!"))
        self.assertEqual(fields, {"captchaText": ["Ab42"], "letter": ["Здравствуйте!"]})


if __name__ == "__main__":
    unittest.main()
