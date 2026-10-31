"""Пауза, возобновление и смена циклов без ожидания реальных четырёх часов."""

import io
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import service
from runtime_control import CycleExpired, RunControl, RunStopped
from openai_client import OpenAIStop
from search_planner import RepeatedSearchQuery


class ServiceClock:
    def __init__(self, stop_at):
        self.current, self.stop_at = 0, stop_at
        self.waits = []

    def now(self):
        return self.current

    def checkpoint(self):
        if self.current >= self.stop_at:
            raise RunStopped()

    def wait_until(self, target):
        if target > self.current:
            self.waits.append(target)
            self.current = target


class RuntimeTests(unittest.TestCase):
    def test_pause_blocks_actions_until_exact_resume_command(self):
        control = RunControl()
        self.addCleanup(control.stop)
        control.handle_command("INeedSomeSleep")
        done = threading.Event()
        thread = threading.Thread(target=lambda: (control.checkpoint(), done.set()), daemon=True)
        thread.start()
        self.assertFalse(done.wait(0.1))
        control.handle_command("unknown")
        self.assertTrue(control.paused)
        control.handle_command("WhyYouSoUgly")
        self.assertTrue(done.wait(1))
        thread.join(timeout=1)

    def test_pause_does_not_extend_the_cycle_clock(self):
        now = [0]
        control = RunControl(clock=lambda: now[0])
        control.handle_command("INeedSomeSleep")
        now[0] = 15
        control.handle_command("WhyYouSoUgly")
        with self.assertRaises(CycleExpired):
            control.checkpoint(deadline=10)

    def test_enter_confirms_only_pending_manual_check_and_keeps_pause(self):
        control = RunControl()
        control.handle_command("")
        self.assertFalse(control.poll_manual_confirmation())
        control.handle_command("INeedSomeSleep")
        control.begin_manual_check()
        control.handle_command("WhyYouSoUgly")
        self.assertFalse(control.poll_manual_confirmation())
        control.handle_command("INeedSomeSleep")
        control.handle_command("")
        self.assertTrue(control.poll_manual_confirmation())
        self.assertTrue(control.paused)
        control.end_manual_check()
        control.handle_command("")
        self.assertFalse(control.poll_manual_confirmation())

    def test_closed_console_or_stop_does_not_hang_manual_check(self):
        control = RunControl()
        thread = control.start_console(io.StringIO(""))
        thread.join(timeout=1)
        control.begin_manual_check()
        with self.assertRaisesRegex(RuntimeError, "Терминал закрыт"):
            control.poll_manual_confirmation()
        control.stop()
        with self.assertRaises(RunStopped):
            control.poll_manual_confirmation()

    def test_scheduler_uses_recovered_page_next_cycle_without_resetting_clock(self):
        class ClockControl:
            current = 0

            def now(self):
                return self.current

            def checkpoint(self):
                if self.current >= 10:
                    raise RunStopped()

            def wait_until(self, target):
                self.current = target

        control = ClockControl()
        current_page = ["first"]
        pages, deadlines = [], []

        def recover(_challenge):
            current_page[0] = "recovered"
            return current_page[0]

        def action(page, **kwargs):
            pages.append(page)
            if kwargs["task"] == "apply":
                deadlines.append(kwargs["deadline"])
                if len(deadlines) == 1:
                    kwargs["on_human_required"](None)
            control.current += 1

        with (patch.object(service, "load_config", return_value={"cycle_hours": 4 / 3600, "max_vacancies": 400}),
              patch.object(service, "run_actions", side_effect=action)):
            service.run_continuous("first", task="all", config_path=Path("unused"), dry_run=False,
                                   control=control, page_provider=lambda: current_page[0], on_human_required=recover)
        self.assertEqual(pages, ["first", "first", "recovered", "recovered"])
        self.assertEqual(deadlines, [5, 10])

    def test_console_reads_commands_without_touching_browser_thread(self):
        control = RunControl()
        thread = control.start_console(io.StringIO("INeedSomeSleep\nWhyYouSoUgly\nINeedSomeSleep\n"))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(control.paused)
        control.stop()
        with self.assertRaises(RunStopped):
            control.checkpoint()

    def test_scheduler_starts_new_cycle_at_boundary(self):
        class ClockControl:
            current = 0
            waits = []

            def now(self):
                return self.current

            def checkpoint(self):
                if self.current >= 10:
                    raise RunStopped()

            def wait_until(self, target):
                self.waits.append(target)
                self.current = target

        control = ClockControl()
        started = []
        bumps = []

        def cycle(*args, **kwargs):
            if kwargs["task"] == "bump":
                bumps.append(control.current)
            else:
                started.append((kwargs["cycle_started"], kwargs["deadline"], kwargs["task"]))
            control.current += 1

        with (patch.object(service, "load_config", return_value={"cycle_hours": 4 / 3600, "max_vacancies": 400}),
              patch.object(service, "run_actions", side_effect=cycle)):
            service.run_continuous(None, task="all", config_path=Path("unused"), dry_run=False, control=control)
        self.assertEqual(started, [(1, 5, "apply"), (6, 10, "apply")])
        self.assertEqual(bumps, [0, 5])
        self.assertEqual(control.waits, [5, 10])

    def test_cli_defaults_to_continuous_all_and_supports_one_pass(self):
        self.assertEqual(main.parse_args([]).task, "all")
        self.assertFalse(main.parse_args([]).once)
        self.assertTrue(main.parse_args(["run", "--once"]).once)

    def test_next_search_starts_immediately_with_shared_cycle_quota_and_deadline(self):
        control = ServiceClock(stop_at=101)
        searches, bumps, progress_objects = [], [], []

        def action(_page, **kwargs):
            if kwargs["task"] == "bump":
                bumps.append(control.current)
            else:
                progress = kwargs["progress"]
                searches.append((control.current, progress.checked, kwargs["deadline"]))
                progress_objects.append(progress)
                progress.checked += 1
            control.current += 1
            return 1

        config = {"cycle_hours": 100 / 3600, "max_vacancies": 3, "max_applications": 3}
        with patch.object(service, "load_config", return_value=config), patch.object(service, "run_actions", side_effect=action):
            service.run_continuous(None, task="all", config_path=Path("unused"), dry_run=False, control=control)
        self.assertEqual(searches, [(1, 0, 101), (2, 1, 101), (3, 2, 101)])
        self.assertEqual(bumps, [0])
        self.assertEqual(control.waits, [101])
        self.assertTrue(all(progress is progress_objects[0] for progress in progress_objects))

    def test_empty_or_repeated_search_waits_before_another_attempt(self):
        control = ServiceClock(stop_at=20)
        calls = []

        def action(_page, **kwargs):
            calls.append(control.current)
            control.current += 1
            if len(calls) == 1:
                raise RepeatedSearchQuery("Повтор запроса")
            return 0

        config = {"cycle_hours": 20 / 3600, "max_vacancies": 400, "empty_search_delay_seconds": 5}
        with patch.object(service, "load_config", return_value=config), patch.object(service, "run_actions", side_effect=action):
            service.run_continuous(None, task="apply", config_path=Path("unused"), dry_run=False, control=control)
        self.assertEqual(calls, [0, 6, 12, 18])
        self.assertEqual(control.waits, [6, 12, 18, 20])

    def test_api_budget_stop_is_not_retried_by_search_loop(self):
        control = ServiceClock(stop_at=100)
        config = {"cycle_hours": 100 / 3600, "max_vacancies": 400}
        with (patch.object(service, "load_config", return_value=config),
              patch.object(service, "run_actions", side_effect=OpenAIStop("Бюджет исчерпан")) as action):
            with self.assertRaises(OpenAIStop):
                service.run_continuous(None, task="apply", config_path=Path("unused"), dry_run=False, control=control)
        action.assert_called_once()
        self.assertEqual(control.waits, [])

    def test_application_quota_is_shared_between_searches(self):
        control = ServiceClock(stop_at=100)

        def action(_page, **kwargs):
            kwargs["progress"].checked += 1
            kwargs["progress"].sent += 1
            control.current += 1
            return 1

        config = {"cycle_hours": 100 / 3600, "max_vacancies": 400, "max_applications": 1}
        with patch.object(service, "load_config", return_value=config), patch.object(service, "run_actions", side_effect=action) as calls:
            service.run_continuous(None, task="apply", config_path=Path("unused"), dry_run=False, control=control)
        calls.assert_called_once()
        self.assertEqual(control.waits, [100])


if __name__ == "__main__":
    unittest.main()
