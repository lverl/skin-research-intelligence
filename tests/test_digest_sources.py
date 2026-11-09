"""DIG-001 checks the real coverage SQL and the saved reader-facing edition."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import digest
import llm
import relevance
from test_reader_relevance import complete_review
from bot.main import format_digest
from collector.main import ingest


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class DigestSourcePostgresTests(unittest.TestCase):
    def setUp(self):
        import db
        import psycopg
        self.schema = "dig001_coverage_" + uuid4().hex
        self.admin = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                    options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=3000")
        db.init_schema(self.conn)
        self.environment = patch.dict(os.environ, {"DIGEST_TIMEZONE": "UTC", "DIGEST_HOUR": "0", "EXA_MONTHLY_USD": "0",
            "MODEL": "offline-editorial-fixture", "OPENROUTER_API_KEY": "offline-fixture",
            "MODEL_MONTHLY_USD": "10", "MODEL_CALL_RESERVE_USD": "1"})
        self.environment.start()
        self.now = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.since = self.now - timedelta(days=1)

    def tearDown(self):
        self.environment.stop()
        self.conn.close()
        self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.close()

    def source(self, kind="direct_pages", url="https://example.org/public", interval=24):
        return self.conn.execute("INSERT INTO sources(name,kind,url,active,interval_hours) VALUES (%s,%s,%s,TRUE,%s) RETURNING id",
                                 ("Fixture " + uuid4().hex, kind, url, interval)).fetchone()[0]

    def checked(self, sid, status="success", *, when=None, purpose="monitoring", material_ids=()):
        from db import jsonb
        moment = when or self.now - timedelta(minutes=1)
        complete = status in {"success", "empty", "no_text"}
        run = self.conn.execute("INSERT INTO runs(service,status,started_at,finished_at) VALUES ('collector','finished',%s,%s) RETURNING id",
                                (moment, moment)).fetchone()[0]
        self.conn.execute("""INSERT INTO source_checks(run_id,source_id,purpose,status,stage,planned,complete,code,reason,details,started_at,finished_at)
                             VALUES (%s,%s,%s,%s,'collection',TRUE,%s,%s,%s,%s,%s,%s)""",
                          (run, sid, purpose, status, complete, None if complete else "timeout",
                           None if complete else "Источник не ответил вовремя", jsonb({"material_ids": list(material_ids), "pages": []}), moment, moment))
        self.conn.execute("UPDATE sources SET last_checked_at=%s WHERE id=%s", (moment, sid))
        return run

    def material(self, sid, *, verified=True, role="research_candidate", kind="research", active_fact=True):
        from db import jsonb
        url = "https://example.org/fixture/" + uuid4().hex
        text = "Synthetic adult facial retinol study reports human clinical results. Twelve fixture observations were recorded."
        mid = ingest(self.conn, {"url": url, "title": "Adult facial retinol research fixture", "text": text,
                                 "text_scope": "excerpt", "source_role": role}, sid)
        self.conn.execute("UPDATE materials SET status='done' WHERE id=%s", (mid,))
        self.conn.execute("""INSERT INTO findings(material_id,summary,model,prompt_version,kind,verified,details)
                             VALUES (%s,'Контрольный материал','fixture','fixture',%s,%s,%s)""",
                          (mid, kind, verified, jsonb({"text_scope": "excerpt", "source_role": role})))
        self.conn.execute("INSERT INTO facts(material_id,statement,quote,active) VALUES (%s,'Synthetic fixture result','Twelve fixture observations were recorded.',%s)",
                          (mid, active_fact))
        with patch.object(llm, "call", return_value=complete_review({"text": text})):
            decision = relevance.ensure_assessment(self.conn, relevance.get_material(self.conn, mid))
        self.assertEqual(decision["status"], "eligible")
        return mid

    def model(self, conn, stage, instructions, data):
        if stage == "digest":
            candidates = data if isinstance(data, list) else data["candidates"]
            return {"items": [{"material_ids": [candidates[0]["id"]], "fact_ids": [f["id"] for f in candidates[0]["facts"]],
                                "title": "Фикстура", "summary": "Подтверждённый пункт фикстуры",
                                "limitations": "Доступен только фрагмент"}]}
        if stage == "digest_verify":
            return {"items": [{"index": i, "valid": True, "code": "supported"} for i in range(len(data["items"]))]}
        raise AssertionError(stage)

    def edition(self, did):
        return self.conn.execute("SELECT status,content FROM digests WHERE id=%s", (did,)).fetchone()

    def test_two_complete_checks_with_zero_updates_allow_no_updates_message(self):
        for _ in range(2):
            self.checked(self.source(), "empty")
        with patch.object(llm, "call") as model:
            did = digest.generate(self.conn, self.now)
        model.assert_not_called()
        status, content = self.edition(did)
        self.assertEqual(status, "ready")
        self.assertTrue(content["coverage"]["complete"])
        self.assertEqual(content["coverage"]["checked"], 2)
        self.assertEqual(content["coverage"]["pending"], 0)
        self.assertRegex(content["message"].lower(), "подходящих обновлений нет|подходящих материалов.*нет")

    def test_missing_check_and_question_only_check_never_mean_no_updates(self):
        sid = self.source()
        for purpose in (None, "question"):
            if purpose:
                self.checked(sid, "empty", purpose=purpose)
            coverage = digest.source_coverage(self.conn, self.since, self.now)
            self.assertFalse(coverage["complete"])
            self.assertIn(sid, coverage["missing_source_ids"])
        with patch.object(llm, "call"):
            did = digest.generate(self.conn, self.now)
        status, content = self.edition(did)
        self.assertEqual(status, "incomplete")
        self.assertRegex(content["message"].lower(), "проверка.*не заверш")

    def test_failure_without_items_then_completed_checks_refreshes_without_model_calls(self):
        sid = self.source()
        self.checked(sid, "failed")
        with patch.object(llm, "call") as model:
            first = digest.generate(self.conn, self.now)
            self.assertEqual(self.edition(first)[0], "incomplete")
            self.checked(sid, "empty", when=self.now + timedelta(seconds=30))
            second = digest.generate(self.conn, self.now + timedelta(minutes=1))
        model.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(self.edition(second)[0], "ready")
        self.assertTrue(self.edition(second)[1]["coverage"]["complete"])

    def test_one_verified_item_with_failed_source_is_partial_and_coverage_refresh_reuses_item(self):
        good, bad = self.source(), self.source()
        mid = self.material(good)
        self.checked(good, material_ids=[mid])
        self.checked(bad, "failed")
        with patch.object(llm, "call", side_effect=self.model):
            did = digest.generate(self.conn, self.now)
        status, first = self.edition(did)
        self.assertEqual(status, "partial")
        self.assertEqual(len(first["items"]), 1)
        self.assertFalse(first["coverage"]["complete"])
        self.assertRegex(format_digest(str(self.now.date()), first).lower(), "не заверш|частич|непол|по части")
        self.checked(bad, "empty", when=self.now + timedelta(seconds=30))
        with patch.object(llm, "call", side_effect=AssertionError("Coverage-only refresh must reuse verified items")):
            again = digest.generate(self.conn, self.now + timedelta(minutes=1))
        self.assertEqual(again, did)
        self.assertEqual(self.edition(again)[0], "ready")
        self.assertEqual(self.edition(again)[1]["items"], first["items"])

    def test_irrelevant_catalogue_and_device_event_failures_do_not_make_coverage_incomplete(self):
        self.checked(self.source(), "empty")
        self.checked(self.source("beauty_facts", "https://world.openbeautyfacts.org/search"), "failed")
        self.checked(self.source("openfda", "https://api.fda.gov/device/event.json"), "failed")
        coverage = digest.source_coverage(self.conn, self.since, self.now)
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["expected"], 1)
        self.assertEqual(coverage["failed"], 0)

    def test_not_due_and_preperiod_deferred_sources_are_outside_plan_but_current_deferral_is_failure(self):
        self.checked(self.source(), "empty")
        not_due = self.source(interval=168)
        self.checked(not_due, "empty", when=self.since - timedelta(hours=1))
        deferred = self.source()
        self.conn.execute("UPDATE sources SET deferred_at=%s,deferred_code='timeout',deferred_reason='Отложен' WHERE id=%s",
                          (self.since - timedelta(hours=1), deferred))
        coverage = digest.source_coverage(self.conn, self.since, self.now)
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["expected"], 1)
        current = self.source()
        self.checked(current, "failed")
        self.conn.execute("UPDATE sources SET deferred_at=%s,deferred_code='timeout',deferred_reason='Отложен' WHERE id=%s",
                          (self.now - timedelta(seconds=30), current))
        self.assertFalse(digest.source_coverage(self.conn, self.since, self.now)["complete"])

    def test_unfinished_daily_claim_or_completed_marker_without_checks_is_incomplete(self):
        self.checked(self.source(), "empty")
        self.source("exa", "https://api.exa.ai/search")
        day = self.now.date()
        self.conn.execute("INSERT INTO daily_search_runs(day,timezone,scope_id,topic_index,status) VALUES (%s,'UTC',%s,%s,'claimed')",
                          (day, "daily:" + str(day), day.toordinal() % 3))
        with patch.dict(os.environ, {"EXA_MONTHLY_USD": "5"}):
            self.assertFalse(digest.source_coverage(self.conn, self.since, self.now)["complete"])
            self.conn.execute("UPDATE daily_search_runs SET status='completed',finished_at=now() WHERE day=%s", (day,))
            self.assertFalse(digest.source_coverage(self.conn, self.since, self.now)["complete"])

    def test_pending_is_scoped_to_checked_materials_and_unverified_science_is_not_published(self):
        sid = self.source()
        old = ingest(self.conn, {"url": "https://example.org/old-failed", "text": "Old unrelated backlog", "text_scope": "excerpt"}, sid)
        self.conn.execute("UPDATE materials SET status='failed' WHERE id=%s", (old,))
        self.checked(sid, "empty")
        self.assertEqual(digest.source_coverage(self.conn, self.since, self.now)["pending"], 0)
        mid = self.material(sid, verified=False)
        self.checked(sid, material_ids=[mid], when=self.now - timedelta(seconds=20))
        self.conn.execute("UPDATE materials SET status='new' WHERE id=%s", (mid,))
        self.assertEqual(digest.source_coverage(self.conn, self.since, self.now)["pending"], 1)
        self.assertFalse(digest.source_coverage(self.conn, self.since, self.now)["complete"])
        self.conn.execute("UPDATE materials SET status='done' WHERE id=%s", (mid,))
        with patch.object(llm, "call") as model:
            did = digest.generate(self.conn, self.now)
        model.assert_not_called()
        self.assertEqual(self.edition(did)[1]["items"], [])

    def test_legacy_aad_role_is_bounded_and_completed_items_are_reused(self):
        sid = self.source()
        mid = self.material(sid, role="professional_information", kind="guideline")
        self.checked(sid, material_ids=[mid])
        with patch.object(llm, "call", side_effect=self.model):
            did = digest.generate(self.conn, self.now)
        content = self.edition(did)[1]
        self.assertEqual(content["items"][0]["sources"][0]["kind"], "expert")
        self.assertEqual(content["items"][0]["sources"][0]["source_role"], "professional_information")
        with patch.object(llm, "call", side_effect=AssertionError("A completed edition must not pay again")):
            again = digest.generate(self.conn, self.now + timedelta(minutes=1))
        self.assertEqual(did, again)
        self.assertEqual(self.edition(again)[1]["items"], content["items"])


if __name__ == "__main__":
    unittest.main()
