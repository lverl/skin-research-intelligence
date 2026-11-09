"""FIX-002 source/result states and safe diagnostics, without external requests."""

import io
import json
import os
import socket
import unittest
import urllib.error
from contextlib import nullcontext
from unittest.mock import Mock, patch
from uuid import uuid4

import llm
import net
import research
from bot.main import format_answer
from collector import adapters
from collector import main as collector
from web.main import Handler


CANARY = "testOnlySensitiveCanary_FIX002"
QUOTE = "Twelve fixture observations were recorded."


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class AnswerDB:
    def __init__(self, candidates=()):
        self.candidates = candidates
        self.saved = []

    def execute(self, sql, args=()):
        if "FROM materials" in sql and "text_scope" in sql:
            return Result(self.candidates)
        if "INSERT INTO answers" in sql:
            self.saved.append(args)
        return Result()


def fact():
    return {"id": 101, "statement": "Synthetic retinol observation", "quote": QUOTE, "payload": {},
            "material_id": 11, "title": "Fixture source", "url": "https://example.org/retinol",
            "kind": "expert", "scope": "excerpt", "source_role": "professional_information"}


def check(status="empty", *, planned=True, complete=True, code="empty_result"):
    return {"source_id": 1, "name": "Fixture source", "status": status, "planned": planned,
            "complete": complete, "code": code, "stage": "collection", "reason": "Контрольная причина"}


def model(conn, stage, instructions, data):
    if stage == "search_plan":
        return {"query": "retinol", "terms": ["retinol"], "subject_identified": True, "clarification": ""}
    if stage == "answer":
        return {"claims": [{"text": "Подтверждённый результат", "fact_ids": [data["facts"][0]["id"]]}],
                "limitations": [], "missing_data": [], "contradictions": []}
    if stage == "answer_verify":
        return {"claims": [{"index": i, "valid": True, "code": "supported"}
                           for i in range(len(data["answer"]["claims"]))], "notes_valid": True}
    raise AssertionError("Unexpected stage: " + stage)


class SafeSourceErrorTests(unittest.TestCase):
    def test_http_dns_timeout_and_unknown_errors_do_not_expose_reflected_secrets(self):
        http = urllib.error.HTTPError("https://example.org/?key=" + CANARY, 403,
                                      "Provider body " + CANARY, {}, io.BytesIO(CANARY.encode()))
        self.addCleanup(http.close)
        cases = ((http, "http_error"), (socket.gaierror(-2, CANARY), "dns_error"),
                 (TimeoutError(CANARY), "timeout"), (ValueError(CANARY), None),
                 (RuntimeError(CANARY), None))
        with patch.dict(os.environ, {"EXA_API_KEY": CANARY, "OPENROUTER_API_KEY": CANARY}):
            for error, expected in cases:
                with self.subTest(kind=type(error).__name__):
                    safe = net.safe_error(error, stage="access")
                    rendered = json.dumps(safe, ensure_ascii=False)
                    self.assertNotIn(CANARY, rendered)
                    self.assertNotIn(type(error).__name__, rendered)
                    self.assertEqual(safe["stage"], "access")
                    self.assertRegex(safe["reason"], "[А-Яа-я]")
                    if expected:
                        self.assertEqual(safe["code"], expected)
        self.assertEqual(net.safe_error(http)["http_status"], 403)

    def test_structured_error_has_a_static_reason_and_keeps_only_http_number(self):
        error = net.SourceError("http_error", http_status=429)
        safe = net.safe_error(error, stage="contents")
        self.assertEqual(safe["code"], "http_error")
        self.assertEqual(safe["http_status"], 429)
        self.assertEqual(safe["stage"], "contents")
        self.assertRegex(str(error), "[А-Яа-я]")

    def test_documented_api_empty_404_is_distinct_from_generic_http_404(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        cases = (({"error": {"code": "NOT_FOUND", "message": "No matches found!"}}, "empty_result"),
                 ({"error": {"code": "NOT_FOUND", "message": CANARY}}, "http_error"))
        for body, expected in cases:
            response = Mock(status=404)
            response.read.return_value = json.dumps(body).encode()
            with self.subTest(expected=expected), patch.object(net.socket, "getaddrinfo", return_value=addresses), \
                    patch.object(net.http.client, "HTTPSConnection") as connection:
                connection.return_value.getresponse.return_value = response
                with self.assertRaises(net.SourceError) as raised:
                    net.fetch("https://api.fda.gov/device/event.json?fixture=1")
            safe = net.safe_error(raised.exception)
            self.assertEqual(safe["code"], expected)
            self.assertNotIn(CANARY, json.dumps(safe))


class AnswerStateTests(unittest.TestCase):
    def run_answer(self, checks, facts=(), *, failed_sources=(), processing_error=None):
        from worker import main as worker
        rows = [(11, "Retinol fixture", QUOTE, "excerpt")] if processing_error else []
        conn = AnswerDB(rows)
        collection = {"material_ids": [11] if processing_error else [], "checks": checks,
                      "failed_sources": list(failed_sources)}
        with patch.object(research, "fact_context", side_effect=lambda *args, **kwargs: list(facts)), \
                patch.object(collector, "search", return_value=collection), patch.object(llm, "call", side_effect=model), \
                patch.object(worker, "process_material", side_effect=processing_error), \
                patch.object(research, "Jsonb", side_effect=lambda value: value):
            result = research.answer(conn, "retinol", owner_telegram_id=42)
        self.assertEqual(len(conn.saved), 1)
        self.assertEqual(conn.saved[0][1]["status"], result["status"])
        self.assertEqual(conn.saved[0][2], 42)
        return result

    def test_successful_empty_search_is_insufficient_data(self):
        result = self.run_answer([check()])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["claims"], [])

    def test_completed_no_text_search_is_insufficient_data(self):
        result = self.run_answer([check("no_text", code="no_text")])
        self.assertEqual(result["status"], "insufficient_data")

    def test_all_source_failures_are_failed_instead_of_insufficient_data(self):
        result = self.run_answer([check("failed", complete=False, code="dns_error")], failed_sources=["Fixture source"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["claims"], [])

    def test_all_processing_failures_are_failed_even_after_successful_search(self):
        result = self.run_answer([check("success", code="")], processing_error=ValueError(CANARY))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["claims"], [])
        self.assertNotIn(CANARY, json.dumps(result, ensure_ascii=False))

    def test_no_checked_channels_cannot_be_a_successful_empty_search(self):
        for checks in ([], [check("disabled", planned=False, complete=False, code="disabled")],
                       [check("deferred", planned=False, complete=False, code="deferred")]):
            with self.subTest(checks=checks):
                self.assertEqual(self.run_answer(checks)["status"], "failed")

    def test_verified_claim_with_partial_source_failure_has_partial_status(self):
        result = self.run_answer([check("success", code=""), check("failed", complete=False, code="timeout")],
                                 facts=[fact()], failed_sources=["Fixture source"])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["claims"])
        self.assertEqual(result["sources"][0]["url"], fact()["url"])
        self.assertTrue(result["limitations"])

    def test_verified_claim_after_complete_checks_is_completed(self):
        result = self.run_answer([check("success", code="")], facts=[fact()])
        self.assertEqual(result["status"], "completed")

    def test_planned_off_exa_does_not_make_an_independent_completed_check_partial(self):
        checks = [check("success", code=""), check("disabled", planned=False, complete=False, code="disabled")]
        self.assertEqual(self.run_answer(checks, facts=[fact()])["status"], "completed")
        self.assertEqual(self.run_answer(checks)["status"], "insufficient_data")

    def test_necessary_clarification_is_saved_with_its_own_status(self):
        conn = AnswerDB()
        plan = {"subject_identified": False, "query": "", "terms": [], "clarification": "Как называется крем?"}
        with patch.object(research, "fact_context", return_value=[]), patch.object(collector, "search") as search, \
                patch.object(llm, "call", return_value=plan), patch.object(research, "Jsonb", side_effect=lambda value: value):
            result = research.answer(conn, "Эффективен ли этот крем?", owner_telegram_id=42)
        search.assert_not_called()
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["clarification"], "Как называется крем?")
        self.assertEqual(conn.saved[0][1]["status"], "needs_clarification")
        self.assertEqual(conn.saved[0][2], 42)

    def test_rejected_answer_is_not_published_as_completed(self):
        conn = AnswerDB()

        def reject(conn, stage, instructions, data):
            if stage == "answer_verify":
                return {"claims": [{"index": i, "valid": False, "code": "unsupported"}
                                   for i in range(len(data["answer"]["claims"]))], "notes_valid": True}
            return model(conn, stage, instructions, data)

        with patch.object(research, "fact_context", return_value=[fact()]), \
                patch.object(collector, "search", return_value={"material_ids": [], "checks": [check("success")], "failed_sources": []}), \
                patch.object(llm, "call", side_effect=reject), patch.object(research, "Jsonb", side_effect=lambda value: value):
            result = research.answer(conn, "retinol", owner_telegram_id=42)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["claims"], [])

    def test_model_answer_timeout_is_saved_as_failed_without_reflected_secret(self):
        conn = AnswerDB()

        def timeout(conn, stage, instructions, data):
            if stage == "answer":
                raise TimeoutError(CANARY)
            return model(conn, stage, instructions, data)

        with patch.object(research, "fact_context", return_value=[fact()]), \
                patch.object(collector, "search", return_value={"material_ids": [], "checks": [check("success")], "failed_sources": []}), \
                patch.object(llm, "call", side_effect=timeout), patch.object(research, "Jsonb", side_effect=lambda value: value):
            result = research.answer(conn, "retinol", owner_telegram_id=42)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["claims"], [])
        self.assertEqual(conn.saved[0][1]["status"], "failed")
        self.assertNotIn(CANARY, json.dumps(result, ensure_ascii=False))

    def test_bot_shows_russian_status_sources_and_limits_without_raw_kind(self):
        partial = {"status": "partial", "claims": [{"text": "Подтверждённый результат", "fact_ids": [101]}],
                   "sources": [fact()], "limitations": ["Часть источников недоступна"]}
        rendered = format_answer(partial)
        self.assertRegex(rendered.lower(), "частич|непол|по части")
        self.assertIn("Подтверждённый результат", rendered)
        self.assertIn(fact()["url"], rendered)
        self.assertIn("Часть источников недоступна", rendered)
        self.assertNotIn("professional_information", rendered)
        self.assertNotIn("expert", rendered)
        empty = format_answer({"status": "completed", "claims": [], "sources": []})
        self.assertNotIn("Ответ готов", empty)
        self.assertRegex(empty.lower(), "недостат|нет проверенных")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class SourceStatusPostgresTests(unittest.TestCase):
    def setUp(self):
        import db
        import psycopg
        self.schema = "fix002_" + uuid4().hex
        self.admin = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                    options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=3000")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.close()

    def direct_source(self, name="American Academy of Dermatology", domain="www.aad.org", path="/public/fixture-retinol"):
        from db import jsonb
        url = "https://" + domain + path
        config = {"domain": domain, "source_role": "professional_information", "limit": 3,
                  "pages": [{"url": url, "topics": ["retinol"]}], "admin_marker": "preserve"}
        sid = self.conn.execute("INSERT INTO sources(name,kind,url,active,interval_hours,config) VALUES (%s,'direct_pages',%s,TRUE,72,%s) RETURNING id",
                                (name, url, jsonb(config))).fetchone()[0]
        return sid, url, config

    def test_failed_aad_and_cochrane_are_deferred_without_changing_configuration(self):
        cases = (("American Academy of Dermatology", "www.aad.org", "/public/fixture-retinol"),
                 ("Cochrane", "www.cochrane.org", "/evidence/fixture-review"))
        for name, domain, path in cases:
            with self.subTest(name=name):
                sid, url, config = self.direct_source(name, domain, path)
                with patch.object(collector, "seed_sources"), \
                        patch.object(adapters, "fetch", side_effect=TimeoutError(CANARY)) as fetch, \
                        patch("collector.exa.post_json", side_effect=AssertionError("Fallback not authorized")), \
                        patch.object(collector.db, "log") as log:
                    first = collector.search(self.conn, "retinol", scope_id="fixture-first", owner_telegram_id=42)
                    first_check = next(row for row in first["checks"] if row["source_id"] == sid)
                    self.assertTrue(first_check["planned"])
                    self.assertFalse(first_check["complete"])
                    self.assertEqual(first_check["status"], "failed")
                    self.assertEqual(first_check["code"], "timeout")
                    self.assertEqual(fetch.call_count, 1)
                    fetch.reset_mock()
                    second = collector.search(self.conn, "retinol", scope_id="fixture-next", owner_telegram_id=42)
                    fetch.assert_not_called()
                second_check = next(row for row in second["checks"] if row["source_id"] == sid)
                self.assertFalse(second_check["planned"])
                self.assertFalse(second_check["complete"])
                self.assertEqual(second_check["status"], "deferred")
                active, interval, actual_config, deferred_at, code, reason = self.conn.execute(
                    "SELECT active,interval_hours,config,deferred_at,deferred_code,deferred_reason FROM sources WHERE id=%s", (sid,)).fetchone()
                self.assertTrue(active)
                self.assertEqual(interval, 72)
                self.assertEqual(actual_config, config)
                self.assertIsNotNone(deferred_at)
                self.assertEqual(code, "timeout")
                self.assertTrue(reason)
                persisted = self.conn.execute("SELECT planned,complete,code,reason,details FROM source_checks WHERE source_id=%s ORDER BY id", (sid,)).fetchall()
                safe = json.dumps(persisted, ensure_ascii=False, default=str) + repr(log.call_args_list)
                self.assertNotIn(CANARY, safe)

    def test_http_dns_and_timeout_causes_stay_distinct_in_database_and_logs_are_safe(self):
        http = urllib.error.HTTPError("https://example.org/?secret=" + CANARY, 429, CANARY, {}, io.BytesIO(CANARY.encode()))
        self.addCleanup(http.close)
        cases = ((http, "http_error"), (socket.gaierror(-2, CANARY), "dns_error"), (TimeoutError(CANARY), "timeout"))
        for error, expected_code in cases:
            with self.subTest(code=expected_code):
                sid, url, config = self.direct_source("Fixture " + expected_code, "example.org", "/" + expected_code)
                with patch.object(collector, "seed_sources"), patch.object(adapters, "fetch", side_effect=error), \
                        patch.object(collector.db, "log") as log, patch.dict(os.environ, {"EXA_API_KEY": CANARY}):
                    result = collector.search(self.conn, "retinol", scope_id="fixture-" + expected_code, owner_telegram_id=42)
                outcome = next(row for row in result["checks"] if row["source_id"] == sid)
                self.assertEqual(outcome["code"], expected_code)
                stored = self.conn.execute("SELECT code,reason,details FROM source_checks WHERE source_id=%s ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
                self.assertEqual(stored[0], expected_code)
                serialized = json.dumps({"result": result, "stored": stored}, ensure_ascii=False, default=str) + repr(log.call_args_list)
                self.assertNotIn(CANARY, serialized)

    def test_explicit_admin_save_reactivates_deferred_source(self):
        import db
        sid, url, config = self.direct_source()
        self.conn.execute("UPDATE sources SET deferred_at=now(),deferred_code='timeout',deferred_reason='Источник отложен' WHERE id=%s", (sid,))
        payload = {"id": sid, "active": True, "interval_hours": 72, "config": config, "url": url}
        data = json.dumps(payload).encode()
        handler = Handler.__new__(Handler)
        handler.path = "/api/sources"
        handler.command = "POST"
        handler.headers = {"Content-Length": str(len(data)), "Content-Type": "application/json", "Host": "fixture.local"}
        handler.rfile = io.BytesIO(data)
        handler.authenticate = Mock(return_value=True)
        handler.respond = Mock()
        with patch.object(db, "connect", return_value=nullcontext(self.conn)):
            handler.do_POST()
        self.assertEqual(handler.respond.call_args.args[0], 200)
        self.assertEqual(self.conn.execute("SELECT active,interval_hours,config,deferred_at,deferred_code,deferred_reason FROM sources WHERE id=%s", (sid,)).fetchone(),
                         (True, 72, config, None, None, None))

    def test_one_failed_aad_page_does_not_defer_another_readable_page(self):
        from db import jsonb
        sid, url, config = self.direct_source()
        second = "https://www.aad.org/public/readable-fixture"
        config["pages"].append({"url": second, "topics": ["retinol"]})
        self.conn.execute("UPDATE sources SET config=%s WHERE id=%s", (jsonb(config), sid))
        html = ("<html><head><title>Retinol public fixture</title></head><body><main>"
                "<p>This synthetic public explanation contains enough readable text to be imported. "
                "It is a patient information page, not a clinical trial or formal guideline.</p>"
                "</main></body></html>").encode()
        with patch.object(collector, "seed_sources"), \
                patch.object(adapters, "fetch", side_effect=[TimeoutError(CANARY), html]):
            result = collector.search(self.conn, "retinol", scope_id="partial-aad-fixture", owner_telegram_id=42)
        outcome = next(row for row in result["checks"] if row["source_id"] == sid)
        self.assertEqual(outcome["status"], "partial")
        self.assertTrue(outcome["planned"])
        self.assertFalse(outcome["complete"])
        self.assertEqual(len(result["material_ids"]), 1)
        self.assertEqual(self.conn.execute("SELECT deferred_at FROM sources WHERE id=%s", (sid,)).fetchone(), (None,))
        self.assertEqual(self.conn.execute("SELECT url FROM materials").fetchone(), (second,))

    def test_exa_disabled_is_unplanned_but_missing_config_under_positive_budget_is_failure(self):
        self.conn.execute("INSERT INTO sources(name,kind,url,active,config) VALUES ('Exa','exa','https://api.exa.ai/search',TRUE,'{}')")
        with patch.object(collector, "seed_sources"), patch("collector.exa.post_json") as transport:
            with patch.dict(os.environ, {"EXA_MONTHLY_USD": "0", "EXA_SCOPE_USD": "0", "EXA_API_KEY": ""}):
                disabled = collector.search(self.conn, "retinol", scope_id="disabled-fixture", owner_telegram_id=42)
            with patch.dict(os.environ, {"EXA_MONTHLY_USD": "5", "EXA_SCOPE_USD": "0.05", "EXA_API_KEY": ""}):
                missing_key = collector.search(self.conn, "retinol", scope_id="missing-key-fixture", owner_telegram_id=42)
            with patch.dict(os.environ, {"EXA_MONTHLY_USD": "5", "EXA_SCOPE_USD": "0.05", "EXA_API_KEY": CANARY,
                                        "EXA_PRICING_DATE": "", "EXA_SEARCH_UPPER_USD": "0", "EXA_CONTENTS_UPPER_USD": "0"}):
                invalid_price = collector.search(self.conn, "retinol", scope_id="invalid-price-fixture", owner_telegram_id=42)
        transport.assert_not_called()
        self.assertFalse(disabled["checks"][0]["planned"])
        self.assertFalse(disabled["checks"][0]["complete"])
        self.assertEqual(disabled["checks"][0]["status"], "disabled")
        self.assertTrue(missing_key["checks"][0]["planned"])
        self.assertFalse(missing_key["checks"][0]["complete"])
        self.assertEqual(missing_key["checks"][0]["code"], "configuration_error")
        self.assertTrue(invalid_price["checks"][0]["planned"])
        self.assertFalse(invalid_price["checks"][0]["complete"])
        self.assertEqual(invalid_price["checks"][0]["code"], "configuration_error")

    def test_exhausted_exa_is_planned_but_blocked_without_transport(self):
        from collector import exa
        self.conn.execute("INSERT INTO sources(name,kind,url,active,config) VALUES ('Exa','exa','https://api.exa.ai/search',TRUE,'{}')")
        settings = {"EXA_MONTHLY_USD": "5", "EXA_SCOPE_USD": "0.05", "EXA_API_KEY": CANARY,
                    "EXA_PRICING_DATE": "2026-11-08", "EXA_SEARCH_UPPER_USD": "0.01", "EXA_CONTENTS_UPPER_USD": "0.01"}
        with patch.dict(os.environ, settings), patch.object(collector, "seed_sources"), \
                patch.object(exa, "reserve_call", side_effect=exa.ExaBudgetExceeded("Лимит Exa исчерпан")), \
                patch.object(exa, "post_json") as transport:
            result = collector.search(self.conn, "retinol", scope_id="quota-fixture", owner_telegram_id=42)
        transport.assert_not_called()
        outcome = result["checks"][0]
        self.assertTrue(outcome["planned"])
        self.assertFalse(outcome["complete"])
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(outcome["code"], "budget_exceeded")

    def test_miniapp_api_and_history_keep_actual_saved_insufficient_status_and_owner(self):
        import db
        data = json.dumps({"question": "retinol"}).encode()
        handler = Handler.__new__(Handler)
        handler.path = "/api/miniapp/ask"
        handler.command = "POST"
        handler.headers = {"Content-Length": str(len(data)), "Content-Type": "application/json", "Host": "fixture.local"}
        handler.rfile = io.BytesIO(data)
        handler.miniapp_user = Mock(return_value=42)
        handler.respond = Mock()
        with patch.object(db, "connect", return_value=nullcontext(self.conn)), \
                patch.object(collector, "search", return_value={"material_ids": [], "checks": [check()], "failed_sources": []}), \
                patch.object(llm, "call", side_effect=model):
            handler.do_POST()
        self.assertEqual(handler.respond.call_args.args[0], 200)
        response = handler.respond.call_args.args[1]
        self.assertEqual(response["status"], "insufficient_data")
        stored = self.conn.execute("SELECT content,owner_telegram_id FROM answers").fetchone()
        self.assertEqual(stored[0]["status"], "insufficient_data")
        self.assertEqual(stored[1], 42)
        handler.path = "/api/miniapp/answers"
        handler.respond.reset_mock()
        with patch.object(db, "connect", return_value=nullcontext(self.conn)):
            handler.do_GET()
        self.assertEqual(handler.respond.call_args.args[0], 200)
        history = handler.respond.call_args.args[1]
        self.assertEqual(history[0]["content"]["status"], response["status"])
        self.assertEqual(history[0]["content"]["source_checks"], response["source_checks"])


if __name__ == "__main__":
    unittest.main()
