import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from actions import search_url
from openai_client import OpenAIError
from search_planner import EXPERIENCE, RepeatedSearchQuery, plan_search
from journal import Journal


PLAN = {"query": "python backend", "search_field": "name", "maximum_experience": "between3And6",
        "level": "senior", "reason": "Подтверждённый опыт разработки Python API."}


class SearchPlannerTests(unittest.TestCase):
    def plan(self, plan=None, **kwargs):
        with patch("search_planner.request_json", return_value={"data": plan or PLAN, "cached": False}):
            return plan_search({"model": "test"}, "Python API, опыт 4 года", location="moscow_any", **kwargs)

    def test_includes_own_experience_band_and_all_lower_bands(self):
        self.assertEqual(self.plan()["experience"], EXPERIENCE[:3])
        self.assertEqual(self.plan(include_lower=False)["experience"], ["between3And6"])
        self.assertEqual(self.plan(dict(PLAN, maximum_experience="noExperience"))["experience"], ["noExperience"])
        self.assertEqual(self.plan(dict(PLAN, maximum_experience="moreThan6"))["experience"], EXPERIENCE)

    def test_model_filters_preserve_moscow_and_remote_user_choices(self):
        plan = self.plan()
        for location, area in (("moscow_any", "1"), ("russia_remote", "113")):
            result = parse_qs(urlparse(search_url({"search_query": plan["query"], "search_location": location,
                "search_filters": {"search_field": plan["search_field"], "experience": plan["experience"],
                                   "area": "other", "work_format": "OFFICE"}})).query)
            self.assertEqual(result["experience"], EXPERIENCE[:3])
            self.assertEqual(result["area"], [area])
            self.assertEqual(result.get("work_format"), ["REMOTE"] if location == "russia_remote" else None)

    def test_unknown_filters_or_url_instead_of_query_are_rejected(self):
        for change in ({"query": ""}, {"query": "https://example.com"}, {"search_field": "everything"},
                       {"maximum_experience": "guru"}, {"level": "unknown"}):
            with self.subTest(change=change), self.assertRaises(OpenAIError):
                self.plan(dict(PLAN, **change))

    def test_new_round_includes_previous_queries_and_changes_cached_request_input(self):
        with patch("search_planner.request_json", return_value={"data": PLAN, "cached": False}) as request:
            plan_search({"model": "test"}, "Python API", location="moscow_any",
                        previous_queries=["Backend developer"], round_number=42)
        data = request.call_args.args[3]
        self.assertEqual(data["previous_queries"], ["Backend developer"])
        self.assertEqual(data["search_round"], 42)

    def test_repeated_phrase_with_different_case_spacing_or_word_order_is_rejected(self):
        for old in ("PYTHON BACKEND", " backend   Python ", "python\xa0backend"):
            with self.subTest(old=old), self.assertRaises(RepeatedSearchQuery):
                self.plan(previous_queries=[old])

    def test_user_examples_are_valid_short_phrases(self):
        for phrase in ("python backend", "python fastapi", "python junior backend", "python разработчик"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.plan(dict(PLAN, query=phrase))["query"], phrase)

    def test_boolean_operators_encoded_symbols_and_long_lists_are_rejected(self):
        for phrase in ("Python FastAPI OR asyncio OR Kafka OR PostgreSQL", "python OR backend",
                       "python AND fastapi", "python NOT java", "python+backend", "python_fastapi",
                       "(python backend)", '"python backend"', "python fastapi django asyncio kafka"):
            with self.subTest(phrase=phrase), self.assertRaises(OpenAIError):
                self.plan(dict(PLAN, query=phrase))

    def test_query_history_and_round_number_survive_restart(self):
        with TemporaryDirectory(prefix="jobfinder-search-history-") as temporary:
            journal = Journal(Path(temporary))
            first = journal.begin_search_round("resume-and-location")
            journal.record_search_plan(first, PLAN)
            journal.close()
            journal = Journal(Path(temporary))
            try:
                self.assertEqual(journal.recent_search_queries("resume-and-location"), [PLAN["query"]])
                self.assertEqual(journal.recent_search_queries("different-resume"), [])
                self.assertGreater(journal.begin_search_round("resume-and-location"), first)
            finally:
                journal.close()
