"""Проверка одного нового отклика: сначала подготовка, затем --send-prepared."""

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import actions
from journal import Journal
from main import Settings, check_session, default_profile, open_browser
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vacancy_id", type=int)
    parser.add_argument("--send-prepared", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    config = actions.load_config(Path(__file__).resolve().parents[1] / "config.json")
    instructions = actions.instructions_from_config(config)
    journal = Journal(config["output"])
    record_path = config["output"] / "checks" / (str(args.vacancy_id) + ".json")
    original_model = actions.generate_letter

    def checked_model(model_config, style, resume, vacancy):
        digest = hashlib.sha256(json.dumps([model_config, style, resume, vacancy], sort_keys=True,
                                           ensure_ascii=False).encode("utf-8")).hexdigest()
        if args.send_prepared:
            cached = json.loads(record_path.read_text(encoding="utf-8"))
            if cached["digest"] != digest:
                raise RuntimeError("Резюме, вакансия или настройки изменились; заново подготовьте письмо.")
            return cached["result"]
        result = original_model(model_config, style, resume, vacancy)
        record_path.parent.mkdir(exist_ok=True)
        record_path.write_text(json.dumps({"digest": digest, "result": result}, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    try:
        with sync_playwright() as playwright, open_browser(
            playwright, Settings(profile=default_profile("msedge")), headless=False,
        ) as context:
            page = context.new_page()
            check_session(page)
            title, resume = actions.read_resume(page, config)
            url = "https://hh.ru/vacancy/" + str(args.vacancy_id)
            actions.navigate(page, url)
            vacancy = {"id": str(args.vacancy_id), "url": url, "title": page.locator("h1").first.inner_text()}
            with patch.object(actions, "generate_letter", side_effect=checked_model):
                sent = actions.apply_one(page, vacancy, config, journal, title, resume, instructions,
                                         dry_run=not args.send_prepared)
            if sent:
                actions.navigate(page, url)
                page.screenshot(path=str(record_path.with_suffix(".png")))
            print(json.dumps({"sent": sent, "status": journal.status(vacancy["id"]),
                              "prepared_result": str(record_path)}, ensure_ascii=False), flush=True)
    finally:
        journal.close()


if __name__ == "__main__":
    main()
