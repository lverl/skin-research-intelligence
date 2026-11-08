"""SRC-003 Exa boundaries and opt-in real PostgreSQL reservation tests."""

import os
import io
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime, timezone
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from collector import exa


SECRET = "testOnlyExaSecret_SRC003"
EXA_ENV = {"EXA_API_KEY": SECRET, "EXA_MONTHLY_USD": "5", "EXA_SCOPE_USD": "0.05",
           "EXA_SEARCH_UPPER_USD": "0.01", "EXA_CONTENTS_UPPER_USD": "0.01",
           "EXA_MAX_RESULTS": "5", "EXA_MAX_CONTENTS": "3", "EXA_PRICING_DATE": "2026-11-08"}


def exa_source():
    return {"id": 31, "name": "Exa", "kind": "exa", "url": "https://api.exa.ai/search",
            "config": {"query": "retinol", "limit": 5},
            "context": {"scope_id": "fixture-question", "purpose": "question", "run_id": None},
            "checks": []}


def search_response(results=None, cost="0.007"):
    return {"results": results if results is not None else [
        {"url": "https://publisher.example/old-retinol-paper", "title": "Old fixture paper",
         "author": "Fixture Scientist", "publishedDate": "2004-02-03T00:00:00Z",
         "highlights": ["A search highlight is not the paper text."]}],
            "costDollars": {"total": float(cost)}}


def contents_response(text="Twelve fixture observations were recorded.", cost="0.001"):
    row = {"url": "https://publisher.example/old-retinol-paper", "title": "Old fixture paper"}
    if text is not None:
        row["text"] = text
    else:
        row["highlights"] = ["A highlight cannot establish a verified fact."]
    return {"results": [row], "costDollars": {"total": float(cost)}}


class ExaBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, EXA_ENV)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_search_then_contents_preserve_original_origin_not_exa_authorship(self):
        with patch.object(exa, "reserve_call", side_effect=[101, 102]) as reserve, \
                patch.object(exa, "finish_call") as finish, \
                patch.object(exa, "post_json", side_effect=[search_response(), contents_response()]) as transport:
            rows = list(exa.read_exa(exa_source(), "retinol"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["url"], "https://publisher.example/old-retinol-paper")
        self.assertEqual(row["text"], "Twelve fixture observations were recorded.")
        self.assertEqual(row["text_scope"], "excerpt")
        self.assertEqual(row["source_role"], "research_candidate")
        self.assertIn("Fixture Scientist", row["authors"])
        self.assertIn("2004", str(row["published_at"]))
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(reserve.call_count, 2)
        self.assertEqual(finish.call_count, 2)
        search_payload = transport.call_args_list[0].args[1]
        self.assertEqual(search_payload["query"], "retinol")
        self.assertFalse(any("date" in key.lower() for key in search_payload),
                         "Scientific discovery must not be restricted to recently published work")

    def test_highlights_without_contents_text_remain_metadata(self):
        with patch.object(exa, "reserve_call", side_effect=[101, 102]), patch.object(exa, "finish_call"), \
                patch.object(exa, "post_json", side_effect=[search_response(), contents_response(text=None)]):
            rows = list(exa.read_exa(exa_source(), "retinol"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "")
        self.assertEqual(rows[0]["text_scope"], "metadata")

    def test_exa_first_aad_patient_page_is_not_a_research_candidate(self):
        url = "https://www.aad.org/public/everyday-care/retinoid-retinol"
        search = search_response([{"url": url, "title": "Public patient fixture"}])
        content = {"results": [{"url": url, "text": "Public patient explanation, not a clinical trial."}],
                   "costDollars": {"total": 0.001}}
        with patch.object(exa, "reserve_call", side_effect=[101, 102]), patch.object(exa, "finish_call"), \
                patch.object(exa, "post_json", side_effect=[search, content]):
            rows = list(exa.read_exa(exa_source(), "retinol"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], url)
        self.assertEqual(rows[0]["source_role"], "professional_information")
        self.assertEqual(rows[0]["text_scope"], "excerpt")

    def test_no_key_never_reserves_or_sends(self):
        with patch.dict(os.environ, {"EXA_API_KEY": ""}), patch.object(exa, "reserve_call") as reserve, \
                patch.object(exa, "post_json") as transport:
            with self.assertRaises(exa.ExaUnavailable):
                list(exa.read_exa(exa_source(), "retinol"))
        reserve.assert_not_called()
        transport.assert_not_called()

    def test_missing_upper_price_disables_transport_instead_of_guessing(self):
        with patch.dict(os.environ, {"EXA_SEARCH_UPPER_USD": "0"}), \
                patch.object(exa, "reserve_call") as reserve, patch.object(exa, "post_json") as transport:
            with self.assertRaises((exa.ExaUnavailable, exa.ExaBudgetExceeded)):
                list(exa.read_exa(exa_source(), "retinol"))
        transport.assert_not_called()
        reserve.assert_not_called()

    def test_no_scope_cannot_bypass_per_question_budget(self):
        source = exa_source()
        source["context"]["scope_id"] = None
        with patch.object(exa, "reserve_call") as reserve, patch.object(exa, "post_json") as transport:
            with self.assertRaises(exa.ExaUnavailable):
                list(exa.read_exa(source, "retinol"))
        reserve.assert_not_called()
        transport.assert_not_called()

    def test_budget_refusal_is_before_transport(self):
        with patch.object(exa, "reserve_call", side_effect=exa.ExaBudgetExceeded("Budget refused")), \
                patch.object(exa, "post_json") as transport:
            with self.assertRaises(exa.ExaBudgetExceeded):
                list(exa.read_exa(exa_source(), "retinol"))
        transport.assert_not_called()

    def test_forbidden_legacy_sites_are_not_requested_as_contents(self):
        candidates = [{"url": url, "title": "Forbidden fixture"} for url in (
            "https://europepmc.org/article/MED/123", "https://pubmed.ncbi.nlm.nih.gov/123/")]
        with patch.object(exa, "reserve_call", return_value=101), patch.object(exa, "finish_call"), \
                patch.object(exa, "post_json", return_value=search_response(candidates)) as transport:
            rows = list(exa.read_exa(exa_source(), "retinol"))
        self.assertEqual(rows, [])
        self.assertEqual(transport.call_count, 1, "Forbidden targets must never reach Contents")

    def test_reflected_transport_secret_is_not_in_safe_failure_or_diagnostics(self):
        source = exa_source()
        with patch.object(exa, "reserve_call", return_value=101), patch.object(exa, "finish_call") as finish, \
                patch.object(exa, "post_json", side_effect=TimeoutError("reflected key=" + SECRET)):
            with self.assertRaises(exa.ExaUnavailable) as raised:
                list(exa.read_exa(source, "retinol"))
        safe_outputs = str(raised.exception) + repr(source["checks"]) + repr(finish.call_args_list)
        self.assertNotIn(SECRET, safe_outputs)
        self.assertNotIn("reflected key", safe_outputs)

    def test_raw_transport_key_is_only_a_header_and_provider_error_is_sanitized(self):
        error = urllib.error.HTTPError("https://api.exa.ai/search?key=" + SECRET, 429,
                                       "Provider reflected " + SECRET, {}, io.BytesIO(SECRET.encode()))
        self.addCleanup(error.close)
        with patch.object(exa, "validate_url"), patch.object(exa.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(exa.ExaUnavailable) as raised:
                exa.post_json("/search", {"query": "fixture"})
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.exa.ai/search")
        self.assertNotIn(SECRET, request.full_url)
        self.assertEqual(dict((key.lower(), value) for key, value in request.header_items())["x-api-key"], SECRET)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn("Provider reflected", str(raised.exception))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class ExaReservationPostgresTests(unittest.TestCase):
    """Real committed ledger, independent connections, and no external transport."""

    def setUp(self):
        import psycopg
        import db
        self.psycopg = psycopg
        self.dsn = os.environ["TEST_DATABASE_URL"]
        self.schema = "src003_exa_" + uuid4().hex
        self.admin = psycopg.connect(self.dsn, autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn = self.connect()
        db.init_schema(self.conn)
        self.environment = patch.dict(os.environ, EXA_ENV)
        self.environment.start()
        self.ledger_connections = patch.object(exa.db, "connect", side_effect=self.connect)
        self.ledger_connections.start()

    def connect(self):
        return self.psycopg.connect(self.dsn, autocommit=True,
                                   options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=3000")

    def tearDown(self):
        self.ledger_connections.stop()
        self.environment.stop()
        self.conn.close()
        self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.close()

    def reserve(self, scope="fixture-question", purpose="question", amount="0.03"):
        return exa.reserve_call(scope_id=scope, purpose=purpose, operation="search", reserved_usd=Decimal(amount))

    def test_two_simultaneous_reservations_cannot_spend_the_same_remainder(self):
        gate = Barrier(2)

        def contender():
            gate.wait(timeout=5)
            try:
                return self.reserve()
            except exa.ExaBudgetExceeded:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        count, reserved = self.conn.execute("SELECT count(*),sum(coalesce(actual_usd,reserved_usd)) FROM search_calls").fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(reserved, Decimal("0.03"))

    def test_next_call_blocked_before_exceeding_scope_remainder(self):
        self.reserve(amount="0.04")
        with self.assertRaises(exa.ExaBudgetExceeded):
            self.reserve(amount="0.02")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search_calls").fetchone()[0], 1)

    def test_question_and_daily_share_monthly_budget(self):
        with patch.dict(os.environ, {"EXA_MONTHLY_USD": "0.05"}):
            self.reserve(scope="question-A", amount="0.03")
            with self.assertRaises(exa.ExaBudgetExceeded):
                self.reserve(scope="daily-A", purpose="daily", amount="0.03")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search_calls").fetchone()[0], 1)

    def test_monthly_budget_uses_utc_boundary_despite_negative_session_timezone(self):
        utc_start_charge = datetime.now(timezone.utc).replace(day=1, hour=0, minute=30, second=0, microsecond=0)
        self.conn.execute("""INSERT INTO search_calls(scope_id,purpose,operation,reserved_usd,actual_usd,status,created_at)
                             VALUES ('daily-at-utc-month-start','daily','search',0.04,0.04,'completed',%s)""",
                          (utc_start_charge,))

        def ledger_with_negative_timezone():
            conn = self.connect()
            conn.execute("SET TIME ZONE 'America/New_York'")
            return conn

        with patch.dict(os.environ, {"EXA_MONTHLY_USD": "0.05"}), \
                patch.object(exa.db, "connect", side_effect=ledger_with_negative_timezone):
            with self.assertRaises(exa.ExaBudgetExceeded):
                self.reserve(scope="question-after-utc-charge", purpose="question", amount="0.02")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search_calls").fetchone()[0], 1)

    def test_completed_zero_cost_does_not_keep_the_original_reserve(self):
        call_id = self.reserve(amount="0.04")
        exa.finish_call(call_id, actual_usd=Decimal("0"))
        self.reserve(amount="0.04")
        total = self.conn.execute("SELECT sum(coalesce(actual_usd,reserved_usd)) FROM search_calls").fetchone()[0]
        self.assertEqual(total, Decimal("0.04"))

    def test_unknown_cost_keeps_pending_reserve_after_timeout(self):
        with patch.dict(os.environ, {"EXA_SEARCH_UPPER_USD": "0.03"}), \
                patch.object(exa, "post_json", side_effect=TimeoutError("reflected " + SECRET)) as transport:
            with self.assertRaises(exa.ExaUnavailable):
                list(exa.read_exa(exa_source(), "retinol"))
        row = self.conn.execute("SELECT reserved_usd,actual_usd FROM search_calls").fetchone()
        self.assertEqual(row, (Decimal("0.03"), None))
        with self.assertRaises(exa.ExaBudgetExceeded):
            self.reserve(amount="0.03")
        self.assertEqual(transport.call_count, 1)

    def test_parent_transaction_rollback_does_not_erase_paid_call_reserve(self):
        self.conn.execute("BEGIN")
        run_id = self.conn.execute("INSERT INTO runs(service,status) VALUES ('fixture','running') RETURNING id").fetchone()[0]
        call_id = exa.reserve_call(scope_id="fixture-uncommitted-run", purpose="question", operation="search",
                                   reserved_usd=Decimal("0.03"), run_id=run_id)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.conn.execute("SELECT reserved_usd FROM search_calls WHERE id=%s", (call_id,)).fetchone()[0], Decimal("0.03"))

    def test_previous_month_is_excluded_but_current_pending_reserve_counts(self):
        self.conn.execute("""INSERT INTO search_calls(scope_id,purpose,operation,reserved_usd,actual_usd,status,created_at)
                             VALUES ('old','question','search',100,100,'completed',date_trunc('month',now())-interval '1 second')""")
        self.reserve(amount="0.03")
        with self.assertRaises(exa.ExaBudgetExceeded):
            self.reserve(amount="0.03")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search_calls").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
