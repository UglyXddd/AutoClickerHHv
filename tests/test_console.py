import io
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from console_logging import ConsoleFormatter, GREEN, ORANGE, RED, RESET, YELLOW, enable_terminal_colors
from journal import Journal


class ConsoleTests(unittest.TestCase):
    def record(self, event=None, *, level=logging.INFO, message="Вакансия"):
        result = logging.LogRecord("hh_browser", level, __file__, 1, message, (), None)
        if event:
            result.hh_event = event
        return result

    def test_results_have_requested_colors_and_always_reset(self):
        formatter = ConsoleFormatter(colors=True)
        for event, color in (("not_suitable", ORANGE), ("applied", GREEN), ("questionnaire", YELLOW),
                             ("ui_error", RED), ("vacancy_error", RED), ("model_error", RED),
                             ("response_blocked", RED), ("submission_unknown", RED)):
            with self.subTest(event=event):
                result = formatter.format(self.record(event))
                self.assertTrue(result.startswith(color))
                self.assertTrue(result.endswith(RESET))

    def test_green_only_after_success_not_preparation_or_preview(self):
        formatter = ConsoleFormatter(colors=True)
        for event in ("vacancy_evaluated", "letter_prepared", "submission_pending", "would_apply",
                      "questionnaire_pending_favorite"):
            self.assertNotIn("\x1b[", formatter.format(self.record(event)))
        self.assertTrue(formatter.format(self.record(level=logging.ERROR)).startswith(RED))
        text = formatter.format(self.record("applied", message="Отправлено\nЗдравствуйте!\nБуду рад обсудить детали."))
        self.assertIn("\nЗдравствуйте!", text)
        self.assertEqual(text.count(RESET), 1)

    def test_redirected_output_has_no_escape_sequences(self):
        self.assertFalse(enable_terminal_colors(io.StringIO()))
        self.assertNotIn("\x1b", ConsoleFormatter(colors=False).format(self.record("applied")))

    def test_journal_events_are_plain_json_with_logging_metadata(self):
        with TemporaryDirectory(prefix="hh-colors-test-") as directory:
            journal = Journal(Path(directory))
            try:
                with self.assertLogs("hh_browser", level="INFO") as logs:
                    journal.event("questionnaire", note="В избранном")
                self.assertEqual(logs.records[0].hh_event, "questionnaire")
                content = (Path(directory) / "events.jsonl").read_text(encoding="utf-8")
                self.assertNotIn("\x1b", content)
                self.assertEqual(json.loads(content)["event"], "questionnaire")
            finally:
                journal.close()
