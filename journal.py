"""Состояние откликов и журнал; отметка pending пишется до отправки."""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


class Journal:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "state.sqlite3")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vacancies (id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS reviews (id TEXT, signature TEXT, checked_at REAL, "
            "score INTEGER, reason TEXT, PRIMARY KEY (id, signature))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS search_rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "context TEXT NOT NULL, started REAL NOT NULL, query TEXT, plan TEXT)"
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def status(self, vacancy_id):
        row = self.db.execute("SELECT status FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
        return row[0] if row else None

    def recent_search_queries(self, context, limit=20):
        return [row[0] for row in self.db.execute(
            "SELECT query FROM search_rounds WHERE context=? AND query IS NOT NULL ORDER BY id DESC LIMIT ?",
            (context, limit))]

    def begin_search_round(self, context):
        cursor = self.db.execute("INSERT INTO search_rounds(context,started) VALUES (?,?)", (context, time.time()))
        self.db.commit()
        return cursor.lastrowid

    def record_search_plan(self, round_id, plan):
        self.db.execute("UPDATE search_rounds SET query=?, plan=? WHERE id=?",
                        (plan["query"], json.dumps(plan, ensure_ascii=False), round_id))
        self.db.commit()

    def reviewed_recently(self, vacancy_id, signature, hours):
        row = self.db.execute(
            "SELECT checked_at FROM reviews WHERE id = ? AND signature = ?", (vacancy_id, signature)
        ).fetchone()
        return bool(row and row[0] > time.time() - hours * 3600)

    def record_review(self, vacancy, signature, score, reason):
        self.db.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?) ON CONFLICT(id, signature) DO UPDATE SET "
            "checked_at=excluded.checked_at, score=excluded.score, reason=excluded.reason",
            (vacancy["id"], signature, time.time(), score, reason),
        )
        self.db.commit()

    def event(self, event, *, vacancy=None, note="", **details):
        record = {"at": datetime.now(timezone.utc).isoformat(), "event": event, "note": note}
        if vacancy:
            record.update({key: vacancy[key] for key in ("id", "title", "url")})
        record.update(details)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        labels = {
            "run_started": "Начал обработку", "run_finished": "Обработка завершена",
            "bump_unavailable": "Поднятие пока недоступно", "would_bump": "Пробный запуск: поднял бы резюме",
            "bump_unconfirmed": "Поднятие не подтверждено", "vacancies_found": "Найдено новых вакансий",
            "bump_scheduled": "Запланировал проверку поднятия",
            "search_started": "Начал общий поиск",
            "search_empty": "По запросу и фильтрам вакансий нет; продолжаю поиск",
            "search_planning": "OpenAI подбирает запрос и фильтры по резюме",
            "search_planned": "Подготовил поиск по резюме",
            "search_page": "Просмотрел страницу поиска", "vacancy_opened": "Открыл вакансию",
            "model_started": "Оцениваю вакансию и готовлю письмо", "vacancy_evaluated": "Оценил вакансию",
            "letter_prepared": "Подготовил сопроводительное письмо",
            "application_request_verified": "Запрос отклика проверен: письмо включено",
            "response_blocked": "Отклик заблокирован до отправки",
            "not_suitable": "Пропустил неподходящую вакансию", "requirements_missing": "Не хватает данных для требований письма",
            "questionnaire_pending_favorite": "Обнаружил анкету; добавляю в избранное",
            "questionnaire": "Вакансия с анкетой добавлена в избранное",
            "would_favorite_questionnaire": "Пробный запуск: добавил бы анкету в избранное",
            "submission_pending": "Отправляю отклик", "submission_unknown": "Результат отправки неизвестен",
            "applied": "Отправил отклик", "already_applied": "Отклик уже был отправлен",
            "would_apply": "Пробный запуск: подготовил отклик", "skipped_history": "Пропустил по истории",
            "unsupported_response": "Пропустил незнакомую форму", "ui_error": "Ошибка страницы",
            "model_error": "Ошибка модели OpenAI", "vacancy_error": "Вакансия пропущена из-за ошибки",
            "cycle_expired": "Закончил пачку по времени",
        }
        if event == "resume_bumped":
            message = f"Резюме {note} поднято"
        else:
            message = labels.get(event, event)
            if vacancy:
                message += f': {vacancy["title"]} — {vacancy["url"]}'
            if "score" in details:
                message += f' | Оценка: {details["score"]}/100'
            if details.get("duration_seconds") is not None:
                message += f' | Модель: {details["duration_seconds"]} с.'
            if note:
                message += f" | {note}"
        if details.get("letter"):
            message += "\nСопроводительное письмо:\n" + details["letter"] + "\n"
        logging.getLogger("hh_browser").info(message, extra={"hh_event": event})

    def mark(self, vacancy, status, note="", **details):
        self.db.execute(
            "INSERT INTO vacancies(id, status) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status", (vacancy["id"], status)
        )
        self.db.commit()
        self.event(status, vacancy=vacancy, note=note, **details)

    def reserve(self, vacancy):
        try:
            self.db.execute("INSERT INTO vacancies VALUES (?, 'submission_pending')", (vacancy["id"],))
            self.db.commit()
        except sqlite3.IntegrityError:
            return False
        self.event("submission_pending", vacancy=vacancy)
        return True
