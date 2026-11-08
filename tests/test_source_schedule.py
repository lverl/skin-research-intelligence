"""DIG-001 durable daily claims and Exa costs, with no external traffic."""

import os
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import Mock, patch
from uuid import uuid4

from collector import exa
from collector import main as collector


EXA_ENV = {"EXA_API_KEY": "testOnlyScheduleKey", "EXA_MONTHLY_USD": "5", "EXA_SCOPE_USD": "0.05",
           "EXA_SEARCH_UPPER_USD": "0.01", "EXA_CONTENTS_UPPER_USD": "0.01",
           "EXA_PRICING_DATE": "2026-11-08", "EXA_MAX_RESULTS": "5", "EXA_MAX_CONTENTS": "3",
           "DIGEST_TIMEZONE": "UTC"}


def collected():
    return {"processed": 0, "material_ids": [], "run_id": None, "failed_sources": [],
            "checks": [{"source_id": 1, "name": "Exa", "status": "empty", "code": "empty_result",
                        "planned": True, "complete": True, "material_ids": []}]}


class DailyScheduleBoundaryTests(unittest.TestCase):
    def test_explicit_zero_budget_is_free_and_does_not_claim_a_day(self):
        with patch.dict(os.environ, {"EXA_MONTHLY_USD": "0", "DIGEST_TIMEZONE": "UTC"}), \
                patch.object(collector.db, "connect") as ledger, patch.object(collector, "collect") as collect:
            result = collector.run_daily_search(Mock(), now=datetime(2026, 11, 8, 20, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["claimed"])
        ledger.assert_not_called()
        collect.assert_not_called()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class DailySchedulePostgresTests(unittest.TestCase):
    def setUp(self):
        import db
        import psycopg
        self.psycopg = psycopg
        self.dsn = os.environ["TEST_DATABASE_URL"]
        self.schema = "dig001_schedule_" + uuid4().hex
        self.admin = psycopg.connect(self.dsn, autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn = self.connect()
        db.init_schema(self.conn)
        self.conn.execute("INSERT INTO sources(name,kind,url,active,config) VALUES ('Exa','exa','https://api.exa.ai/search',TRUE,'{}')")
        self.environment = patch.dict(os.environ, EXA_ENV)
        self.environment.start()
        self.ledger = patch.object(collector.db, "connect", side_effect=self.connect)
        self.ledger.start()
        self.seed = patch.object(collector, "seed_sources")
        self.seed.start()
        self.now = datetime(2026, 11, 8, 20, tzinfo=timezone.utc)

    def connect(self):
        return self.psycopg.connect(self.dsn, autocommit=True,
                                   options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=3000")

    def tearDown(self):
        self.seed.stop()
        self.ledger.stop()
        self.environment.stop()
        self.conn.close()
        self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.close()

    def test_two_workers_claim_only_one_paid_daily_attempt(self):
        start = Barrier(2)
        entered = Event()
        release = Event()

        def collect(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("The second worker did not observe the committed daily claim")
            return collected()

        def run():
            with self.connect() as conn:
                start.wait(timeout=5)
                return collector.run_daily_search(conn, now=self.now)

        with patch.object(collector, "collect", side_effect=collect) as paid_attempt, \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            try:
                self.assertTrue(entered.wait(5))
                done, pending = wait(futures, timeout=3, return_when=FIRST_COMPLETED)
                self.assertTrue(done, "An existing committed claim must return without waiting for HTTP")
            finally:
                release.set()
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(paid_attempt.call_count, 1)
        self.assertEqual(sum(result["claimed"] for result in results), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM daily_search_runs").fetchone()[0], 1)

    def test_daily_claim_survives_parent_rollback(self):
        self.conn.execute("BEGIN")
        run_id = self.conn.execute("INSERT INTO runs(service,status) VALUES ('outer-fixture','running') RETURNING id").fetchone()[0]
        result = {**collected(), "run_id": run_id}
        with patch.object(collector, "collect", return_value=result) as collect:
            first = collector.run_daily_search(self.conn, now=self.now)
            self.conn.execute("ROLLBACK")
            again = collector.run_daily_search(self.conn, now=self.now)
        self.assertTrue(first["claimed"])
        self.assertFalse(again["claimed"])
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM daily_search_runs").fetchone()[0], 1)

    def test_timeout_is_not_retried_by_another_pass_of_the_same_day(self):
        with patch.object(collector, "collect", side_effect=TimeoutError("testOnlyScheduleCanary")) as collect:
            first = collector.run_daily_search(self.conn, now=self.now)
            second = collector.run_daily_search(self.conn, now=self.now + timedelta(minutes=1))
        self.assertEqual(first["status"], "failed")
        self.assertFalse(second["claimed"])
        self.assertEqual(collect.call_count, 1)
        row = self.conn.execute("SELECT status,code,reason FROM daily_search_runs").fetchone()
        self.assertEqual(row[0], "failed")
        self.assertNotIn("testOnlyScheduleCanary", repr(row))

    def test_daily_period_is_captured_once_in_configured_timezone(self):
        captured = datetime(2026, 11, 9, 2, 30, tzinfo=timezone.utc)  # Nov 8 in New York.

        def change_configuration(*args, **kwargs):
            os.environ["DIGEST_TIMEZONE"] = "UTC"
            return collected()

        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "America/New_York"}), \
                patch.object(collector, "collect", side_effect=change_configuration):
            result = collector.run_daily_search(self.conn, now=captured)
        self.assertEqual(str(result["day"]), "2026-11-08")
        self.assertEqual(result["scope_id"], "daily:2026-11-08")
        row = self.conn.execute("SELECT day,timezone,scope_id FROM daily_search_runs").fetchone()
        self.assertEqual(str(row[0]), "2026-11-08")
        self.assertEqual(row[1:], ("America/New_York", "daily:2026-11-08"))

    def test_three_topic_rotation_and_next_day_are_not_affected_by_user_question(self):
        with patch.object(collector, "collect", return_value=collected()) as collect:
            first = collector.run_daily_search(self.conn, now=self.now)
            # A manual question may run its own search, but may not reset a daily marker.
            collector.search(self.conn, "retinol", scope_id="question-fixture", owner_telegram_id=42)
            repeated = collector.run_daily_search(self.conn, now=self.now)
            second = collector.run_daily_search(self.conn, now=self.now + timedelta(days=1))
            third = collector.run_daily_search(self.conn, now=self.now + timedelta(days=2))
        self.assertFalse(repeated["claimed"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM daily_search_runs").fetchone()[0], 3)
        for index, result in enumerate((first, second, third)):
            day = (self.now + timedelta(days=index)).date()
            self.assertEqual(result["topic_index"], day.toordinal() % 3)
        self.assertEqual(len({first["topic_index"], second["topic_index"], third["topic_index"]}), 3)
        daily_calls = [call for call in collect.call_args_list if call.kwargs.get("purpose") == "daily"]
        self.assertEqual(len(daily_calls), 3)
        self.assertEqual(len({call.kwargs["query"] for call in daily_calls}), 3)

    def test_daily_collect_only_uses_exa_and_questions_share_its_real_monthly_ledger(self):
        # A non-Exa source must not be called by daily discovery.
        self.conn.execute("INSERT INTO sources(name,kind,url,active,config) VALUES ('Independent fixture','direct_pages','https://example.org/public',TRUE,'{}')")
        exa.reserve_call(scope_id="question-reserve", purpose="question", operation="search", reserved_usd=Decimal("0.045"))
        with patch.dict(os.environ, {"EXA_MONTHLY_USD": "0.05"}), patch.object(exa, "post_json") as transport, \
                patch.dict(collector.READERS, {"direct_pages": Mock(side_effect=AssertionError("Daily search called an independent monitor"))}):
            result = collector.run_daily_search(self.conn, now=self.now)
        transport.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search_calls").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT sum(coalesce(actual_usd,reserved_usd)) FROM search_calls").fetchone()[0], Decimal("0.045"))

    def test_positive_budget_missing_key_claims_one_failed_attempt_and_never_sends(self):
        with patch.dict(os.environ, {"EXA_API_KEY": ""}), patch.object(exa, "post_json") as transport:
            first = collector.run_daily_search(self.conn, now=self.now)
            second = collector.run_daily_search(self.conn, now=self.now)
        transport.assert_not_called()
        self.assertTrue(first["claimed"])
        self.assertEqual(first["status"], "failed")
        self.assertFalse(second["claimed"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM daily_search_runs").fetchone()[0], 1)

    def test_first_daily_search_imports_text_and_repeat_sends_nothing(self):
        url = "https://publisher.example/daily-fixture"
        search = {"results": [{"url": url, "title": "Daily fixture"}], "costDollars": {"total": 0.007}}
        contents = {"results": [{"url": url, "text": "Readable synthetic daily material."}], "costDollars": {"total": 0.001}}
        with patch.object(exa, "post_json", side_effect=[search, contents]) as transport:
            first = collector.run_daily_search(self.conn, now=self.now)
            second = collector.run_daily_search(self.conn, now=self.now)
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(self.conn.execute("SELECT url,text FROM materials").fetchone(),
                         (url, "Readable synthetic daily material."))

    def test_uncaught_crash_keeps_claim_and_prevents_a_second_paid_attempt(self):
        with patch.object(collector, "collect", side_effect=SystemExit("Synthetic worker crash")) as collect:
            with self.assertRaises(SystemExit):
                collector.run_daily_search(self.conn, now=self.now)
            second = collector.run_daily_search(self.conn, now=self.now)
        self.assertFalse(second["claimed"])
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT status FROM daily_search_runs").fetchone()[0], "claimed")


if __name__ == "__main__":
    unittest.main()
