import threading
import unittest
from datetime import datetime
from unittest.mock import Mock

from hh_ui import HumanRequired
from resume_bumps import MOSCOW, ResumeBumpSchedule, next_bump_delay
from runtime_control import CycleExpired, RunControl


class ResumeTimerTests(unittest.TestCase):
    def test_reads_real_hh_message_including_nonbreaking_spaces(self):
        now = datetime(2026, 10, 30, 15, 15, tzinfo=MOSCOW)
        text = "Python Backend-Разработчик\nПоднять вручную можно\xa0сегодня\xa0в\xa018:16\nСтатистика за неделю"
        self.assertEqual(next_bump_delay(text, now=now), 3 * 3600 + 60)

    def test_today_tomorrow_and_expired_time(self):
        now = datetime(2026, 10, 30, 23, 50, tzinfo=MOSCOW)
        self.assertEqual(next_bump_delay("Поднять вручную можно завтра в 00:10", now=now), 1200)
        self.assertEqual(next_bump_delay("Поднять вручную можно сегодня в 23:55", now=now), 300)
        self.assertEqual(next_bump_delay("Поднять вручную можно сегодня в 23:49", now=now), 0)
        self.assertEqual(next_bump_delay("Поднять вручную можно 31.10.2026 в 00:10", now=now), 1200)

    def test_relative_timer_and_unknown_or_paid_messages(self):
        self.assertEqual(next_bump_delay("Поднять в поиске можно через 2 часа 15 минут 1 секунду"), 8101)
        self.assertEqual(next_bump_delay("Поднять вручную можно через 1 минуту"), 60)
        self.assertIsNone(next_bump_delay("Поднять автоматически за 799 рублей"))
        self.assertIsNone(next_bump_delay("Резюме скрыто"))
        self.assertIsNone(next_bump_delay("Поднять вручную можно сегодня в 25:90"))


class ResumeScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = [0]
        self.control = RunControl(clock=lambda: self.now[0])
        self.addCleanup(self.control.stop)

    def test_checks_at_site_timer_without_waiting_four_hours_and_handles_reentry(self):
        checked = []

        def check():
            checked.append(self.now[0])
            self.control.checkpoint()
            return 7200

        schedule = ResumeBumpSchedule(self.control, check)
        schedule.observe(45 * 60)
        with self.control.maintenance(schedule.poll):
            self.now[0] = 2699
            self.control.checkpoint()
            self.assertEqual(checked, [])
            self.now[0] = 2700
            self.control.checkpoint()
        self.assertEqual(checked, [2700])
        self.assertEqual(schedule.next_check, 9900)

    def test_wait_loop_services_due_bump(self):
        checked = []
        schedule = ResumeBumpSchedule(self.control, lambda: checked.append(self.now[0]) or 100)
        schedule.observe(5)
        self.now[0] = 5
        with self.control.maintenance(schedule.poll):
            self.control.wait_until(5)
        self.assertEqual(checked, [5])

    def test_guard_or_captcha_defers_check_until_safe_point(self):
        blocked = [True]
        check = Mock(return_value=100)
        schedule = ResumeBumpSchedule(self.control, check, blocked=lambda: blocked[0])
        schedule.observe(5)
        self.now[0] = 10
        with self.control.maintenance(schedule.poll):
            self.control.checkpoint()
            check.assert_not_called()
            blocked[0] = False
            self.control.checkpoint()
        check.assert_called_once()

    def test_maintenance_does_not_run_during_pause_or_on_another_thread(self):
        check = Mock(return_value=100)
        schedule = ResumeBumpSchedule(self.control, check)
        schedule.observe(5)
        self.now[0] = 5
        finished = threading.Event()
        with self.control.maintenance(schedule.poll):
            self.control.handle_command("INeedSomeSleep")
            thread = threading.Thread(target=lambda: (self.control.checkpoint(), finished.set()), daemon=True)
            thread.start()
            self.assertFalse(finished.wait(0.05))
            check.assert_not_called()
            self.control.handle_command("WhyYouSoUgly")
            self.assertTrue(finished.wait(1))
            thread.join(1)
            check.assert_not_called()
            self.control.checkpoint()
        check.assert_called_once()

    def test_deadline_is_checked_again_after_bump_check(self):
        def check():
            self.now[0] = 20
            return 100

        schedule = ResumeBumpSchedule(self.control, check)
        schedule.observe(5)
        self.now[0] = 5
        with self.control.maintenance(schedule.poll), self.assertRaises(CycleExpired):
            self.control.checkpoint(deadline=10)

    def test_unknown_timer_retries_in_a_minute_and_access_block_is_not_ignored(self):
        schedule = ResumeBumpSchedule(self.control, Mock(return_value=None))
        schedule.observe(None)
        self.assertEqual(schedule.next_check, 60)
        self.now[0] = 60
        schedule.check.side_effect = RuntimeError("Не загрузилась страница")
        schedule.poll()
        self.assertEqual(schedule.next_check, 120)
        self.now[0] = 120
        schedule.check.side_effect = HumanRequired("HTTP 403", kind="access_denied")
        with self.assertRaises(HumanRequired):
            schedule.poll()
