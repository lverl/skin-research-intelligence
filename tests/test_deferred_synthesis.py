"""FIX-009: deferred public synthesis on real PostgreSQL, without live calls."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import db
import llm
import relevance
import research
from collector import main as collector
from worker import main as worker
from bot import main as bot
from net import ERROR_REASONS
import test_reader_processing as processing_fixture
from test_reader_relevance import NEGATIVE_CASES, complete_review


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class DeferredSynthesisPostgresTests(unittest.TestCase):
    insert = processing_fixture.ReaderProcessingPostgresTests.insert
    row = processing_fixture.ReaderProcessingPostgresTests.row
    model = processing_fixture.ReaderProcessingPostgresTests.model

    def setUp(self):
        processing_fixture.ReaderProcessingPostgresTests.setUp(self)
        self.now = datetime.now(timezone.utc) + timedelta(seconds=2)
        self.topic = "facial pigmentation"

    def tearDown(self):
        processing_fixture.ReaderProcessingPostgresTests.tearDown(self)

    def other_connection(self):
        import psycopg
        return psycopg.connect(
            os.environ["TEST_DATABASE_URL"], autocommit=True,
            options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=3000",
        )

    def prepare(self, count=1, *, topic=None):
        topic = topic or self.topic
        candidates = []
        for index in range(count):
            candidate = self.insert(collected=True, suffix="deferred-" + str(index))
            with patch.object(llm, "call", side_effect=self.model(candidate, subject=topic)) as calls:
                report = worker.process_material(self.conn, self.row(candidate), force=True)
            self.assertEqual(report["facts_saved"], 1)
            self.assertFalse(any(c.args[1].startswith("synthesis") for c in calls.call_args_list))
            candidates.append(candidate)
        return candidates

    def queue(self, topic=None, conn=None):
        fields = ("topic", "revision", "done_revision", "state", "success_signature",
                  "attempt_signature", "attempts", "next_retry_at", "last_error")
        row = (conn or self.conn).execute(
            "SELECT " + ",".join(fields) + " FROM topic_refresh WHERE topic=%s", (topic or self.topic,),
        ).fetchone()
        return dict(zip(fields, row)) if row else None

    def current(self, topic=None):
        return self.conn.execute(
            """SELECT version,content,input_signature,profile_version FROM conclusions
               WHERE topic=%s AND is_current ORDER BY version DESC""", (topic or self.topic,),
        ).fetchall()

    @staticmethod
    def generated(conn, stage, instructions, data):
        if stage == "synthesis":
            return {"claims": [{"text": "Synthetic supported observation " + str(f["id"]), "fact_ids": [f["id"]]}
                               for f in data["facts"]],
                    "limitations": [], "contradictions": [], "missing_data": []}
        if stage == "synthesis_verify":
            return {"valid": True}
        raise AssertionError("Unexpected deferred model stage: " + stage)

    def refresh(self, at=None, *, conn=None):
        return research.refresh_topics(conn or self.conn, now=at or self.now)

    def mark(self, topics=None, *, conn=None):
        connection = conn or self.conn
        with connection.transaction():
            db.mark_topics(connection, topics or [self.topic])

    def test_two_materials_of_one_topic_get_one_synthesis_and_one_verification(self):
        self.prepare(2)
        before = self.queue()
        self.assertGreater(before["revision"], before["done_revision"])
        with patch.object(llm, "call", side_effect=self.generated) as model:
            reports = self.refresh()
        self.assertTrue(any(item["topic"] == self.topic for item in reports))
        self.assertEqual([call.args[1] for call in model.call_args_list], ["synthesis", "synthesis_verify"])
        current = self.current()
        self.assertEqual(len(current), 1)
        self.assertEqual(len(current[0][1]["claims"]), 2)
        after = self.queue()
        self.assertEqual(after["state"], "ready")
        self.assertEqual(after["revision"], after["done_revision"])
        self.assertEqual(current[0][2], after["success_signature"])
        self.assertEqual(str(current[0][3]), str(relevance.load_profile()["version"]))

    def test_successful_unchanged_input_uses_cache_even_after_new_mark_and_restart(self):
        self.prepare()
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh()
        original = self.current()[0]
        self.mark()
        self.assertEqual(self.current(), [], "Dirty mark must atomically hide the previous current result")
        with self.other_connection() as restarted, \
                patch.object(llm, "call", side_effect=AssertionError("Unchanged successful input must use the cache")) as model:
            self.refresh(self.now + timedelta(minutes=1), conn=restarted)
            research.synthesize(restarted, self.topic, now=self.now + timedelta(minutes=2))
            model.assert_not_called()
        self.assertEqual(self.current(), [original])
        self.assertEqual(self.queue()["done_revision"], self.queue()["revision"])

    def test_processing_facts_and_topic_mark_roll_back_together(self):
        candidate = self.insert(collected=True)
        with patch.object(llm, "call", side_effect=self.model(candidate)):
            with self.assertRaisesRegex(RuntimeError, "rollback marker"):
                with self.conn.transaction():
                    worker.process_material(self.conn, self.row(candidate), force=True)
                    self.assertIsNotNone(self.queue())
                    raise RuntimeError("rollback marker")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM findings").fetchone()[0], 0)
        self.assertIsNone(self.queue())
        self.assertNotEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "eligible")

    def test_invalidating_a_current_result_and_mark_can_roll_back_atomically(self):
        self.prepare()
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh()
        queue_before, current_before = self.queue(), self.current()
        with self.assertRaisesRegex(RuntimeError, "rollback marker"):
            with self.conn.transaction():
                db.mark_topics(self.conn, [self.topic])
                self.assertEqual(self.current(), [])
                raise RuntimeError("rollback marker")
        self.assertEqual(self.queue(), queue_before)
        self.assertEqual(self.current(), current_before)

    def test_reprocessing_marks_both_old_and_new_topic(self):
        candidate = self.prepare()[0]
        original_revision = self.queue()["revision"]
        new_topic = "facial cosmetic safety"
        with patch.object(llm, "call", side_effect=self.model(candidate, subject=new_topic)):
            worker.process_material(self.conn, self.row(candidate), force=True)
        self.assertGreater(self.queue()["revision"], original_revision)
        self.assertIsNotNone(self.queue(new_topic))
        self.assertGreater(self.queue(new_topic)["revision"], self.queue(new_topic)["done_revision"])
        topics = self.conn.execute("SELECT topics FROM findings WHERE material_id=%s", (candidate["id"],)).fetchone()[0]
        self.assertEqual(topics, [new_topic])

    def test_reader_admission_revocation_marks_and_retires_current_without_llm(self):
        candidate = self.prepare()[0]
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh()
        original_revision = self.queue()["revision"]
        revoked = relevance.assess(candidate, review=dict(complete_review(candidate), relevant=False,
                                  decision="excluded", reason="Synthetic reader-scope exclusion for this fixture"))
        self.assertEqual(revoked["status"], "excluded")
        relevance.store_assessment(self.conn, candidate, revoked)
        self.assertGreater(self.queue()["revision"], original_revision)
        self.assertEqual(self.current(), [])
        with patch.object(llm, "call", side_effect=AssertionError("No admitted facts remain")) as model:
            self.refresh(self.now + timedelta(minutes=1))
            model.assert_not_called()
        self.assertFalse(any(row[1].get("claims") for row in self.current()))
        self.assertEqual(self.queue()["done_revision"], self.queue()["revision"])

    def test_failed_reader_revocation_rolls_back_admission_and_mark(self):
        candidate = self.prepare()[0]
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh()
        before_queue, before_current = self.queue(), self.current()
        revoked = relevance.assess(candidate, review=dict(complete_review(candidate), relevant=False,
                                  decision="excluded", reason="Synthetic reader-scope exclusion for this fixture"))
        with self.assertRaisesRegex(RuntimeError, "rollback marker"):
            with self.conn.transaction():
                relevance.store_assessment(self.conn, candidate, revoked)
                self.assertEqual(self.current(), [])
                raise RuntimeError("rollback marker")
        self.assertEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "eligible")
        self.assertEqual(self.queue(), before_queue)
        self.assertEqual(self.current(), before_current)

    def test_new_fact_during_generation_keeps_the_new_work_pending_and_no_stale_current(self):
        candidate = self.prepare()[0]
        changed = False

        def model(conn, stage, instructions, data):
            nonlocal changed
            if stage == "synthesis" and not changed:
                changed = True
                with self.other_connection() as other, other.transaction():
                    other.execute("INSERT INTO facts(material_id,statement,quote) VALUES (%s,%s,%s)",
                                  (candidate["id"], self.topic + ": another synthetic observation",
                                   complete_review(candidate)["evidence_quote"]))
                    db.mark_topics(other, [self.topic])
            return self.generated(conn, stage, instructions, data)

        with patch.object(llm, "call", side_effect=model):
            self.refresh()
        self.assertTrue(changed)
        self.assertEqual(self.current(), [])
        self.assertGreater(self.queue()["revision"], self.queue()["done_revision"])
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh(self.now + timedelta(seconds=1))
        self.assertEqual(len(self.current()[0][1]["claims"]), 2)

    def test_admission_revoked_during_generation_does_not_publish_old_claim(self):
        candidate = self.prepare()[0]
        revoked = relevance.assess(candidate, review=dict(complete_review(candidate), relevant=False,
                                  decision="excluded", reason="Synthetic reader-scope exclusion for this fixture"))
        changed = False

        def model(conn, stage, instructions, data):
            nonlocal changed
            if stage == "synthesis" and not changed:
                changed = True
                with self.other_connection() as other:
                    relevance.store_assessment(other, candidate, revoked)
            return self.generated(conn, stage, instructions, data)

        with patch.object(llm, "call", side_effect=model):
            self.refresh()
        self.assertTrue(changed)
        self.assertEqual(self.current(), [])
        self.assertGreater(self.queue()["revision"], self.queue()["done_revision"])

    def test_another_worker_holding_topic_lock_prevents_duplicate_calls(self):
        self.prepare()
        with self.other_connection() as other:
            other.execute("SELECT pg_advisory_lock(4218,hashtext(%s))", (self.topic,))
            try:
                with patch.object(llm, "call", side_effect=AssertionError("Other worker owns the topic")) as model:
                    self.refresh()
                    model.assert_not_called()
            finally:
                other.execute("SELECT pg_advisory_unlock(4218,hashtext(%s))", (self.topic,))
        self.assertGreater(self.queue()["revision"], self.queue()["done_revision"])
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh()
        self.assertEqual(self.queue()["state"], "ready")

    def test_errors_have_three_attempts_fifteen_minute_pause_and_persist_after_restart(self):
        self.prepare()
        marker = "UNTRUSTED_MODEL_FAILURE_SENTINEL"
        with patch.object(llm, "call", side_effect=RuntimeError(marker)) as model:
            for attempt in range(3):
                at = self.now + timedelta(minutes=15 * attempt)
                self.refresh(at)
                row = self.queue()
                self.assertEqual(row["attempts"], attempt + 1)
                self.assertGreaterEqual(row["next_retry_at"], at + timedelta(minutes=15))
                self.assertNotIn(marker, str(row["last_error"]))
                count = model.call_count
                with self.other_connection() as restarted:
                    self.refresh(at + timedelta(minutes=14, seconds=59), conn=restarted)
                self.assertEqual(model.call_count, count)
            self.assertEqual(model.call_count, 3)
            self.mark()
            self.refresh(self.now + timedelta(hours=6))
            self.assertEqual(model.call_count, 3, "An unchanged-input mark must not reset exhausted attempts")
        row = self.queue()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempts"], 3)
        self.assertGreater(row["revision"], row["done_revision"])

    def test_changed_input_can_retry_without_waiting_for_old_failed_snapshot(self):
        candidate = self.prepare()[0]
        with patch.object(llm, "call", side_effect=RuntimeError("Synthetic failure")):
            self.refresh()
        with self.conn.transaction():
            self.conn.execute("INSERT INTO facts(material_id,statement,quote) VALUES (%s,%s,%s)",
                              (candidate["id"], self.topic + ": new synthetic observation",
                               complete_review(candidate)["evidence_quote"]))
            db.mark_topics(self.conn, [self.topic])
        with patch.object(llm, "call", side_effect=self.generated) as model:
            self.refresh(self.now + timedelta(seconds=1))
        self.assertEqual([call.args[1] for call in model.call_args_list], ["synthesis", "synthesis_verify"])
        self.assertEqual(self.queue()["state"], "ready")
        self.assertEqual(len(self.current()[0][1]["claims"]), 2)

    def test_historical_successful_snapshot_is_reused_after_a_b_a_input_sequence(self):
        candidate = self.prepare()[0]
        fid, original_payload = self.conn.execute(
            "SELECT id,payload FROM facts WHERE material_id=%s AND active", (candidate["id"],),
        ).fetchone()
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh()
        original_current = self.current()[0]
        with self.conn.transaction():
            self.conn.execute("UPDATE facts SET payload=%s WHERE id=%s", (db.jsonb(dict(original_payload, revision="B")), fid))
            db.mark_topics(self.conn, [self.topic])
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh(self.now + timedelta(seconds=1))
        self.assertNotEqual(self.current()[0][2], original_current[2])
        with self.conn.transaction():
            self.conn.execute("UPDATE facts SET payload=%s WHERE id=%s", (db.jsonb(original_payload), fid))
            db.mark_topics(self.conn, [self.topic])
        with patch.object(llm, "call", side_effect=AssertionError("Known successful snapshot A must be reused")) as model:
            self.refresh(self.now + timedelta(seconds=2))
            model.assert_not_called()
        self.assertEqual(self.current(), [original_current])
        self.assertEqual(self.queue()["done_revision"], self.queue()["revision"])

    def test_historical_exhausted_snapshot_cannot_get_a_fourth_attempt_after_a_b_a(self):
        candidate = self.prepare()[0]
        fid, original_payload = self.conn.execute(
            "SELECT id,payload FROM facts WHERE material_id=%s AND active", (candidate["id"],),
        ).fetchone()
        with patch.object(llm, "call", side_effect=RuntimeError("Synthetic snapshot A failure")) as calls:
            for attempt in range(3):
                self.refresh(self.now + timedelta(minutes=15 * attempt))
            self.assertEqual(calls.call_count, 3)
        original_signature = self.queue()["attempt_signature"]
        with self.conn.transaction():
            self.conn.execute("UPDATE facts SET payload=%s WHERE id=%s", (db.jsonb(dict(original_payload, revision="B")), fid))
            db.mark_topics(self.conn, [self.topic])
        with patch.object(llm, "call", side_effect=self.generated):
            self.refresh(self.now + timedelta(minutes=31))
        self.assertEqual(self.queue()["state"], "ready")
        with self.conn.transaction():
            self.conn.execute("UPDATE facts SET payload=%s WHERE id=%s", (db.jsonb(original_payload), fid))
            db.mark_topics(self.conn, [self.topic])
        with patch.object(llm, "call", side_effect=AssertionError("Snapshot A has already exhausted three attempts")) as model:
            self.refresh(self.now + timedelta(hours=6))
            model.assert_not_called()
        self.assertEqual(self.queue()["attempt_signature"], original_signature)
        self.assertEqual(self.queue()["attempts"], 3)
        self.assertEqual(self.queue()["state"], "failed")

    def test_missing_model_or_key_before_http_does_not_consume_attempts_and_can_recover(self):
        self.prepare()
        for offset, settings in enumerate(({"MODEL": ""}, {"OPENROUTER_API_KEY": ""})):
            with self.subTest(settings=settings), patch.dict(os.environ, settings), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("Missing configuration cannot send HTTP")) as http:
                for attempt in range(3):
                    self.refresh(self.now + timedelta(hours=offset * 3 + attempt))
                    self.assertEqual(self.queue()["attempts"], 0)
                http.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM model_calls").fetchone()[0], 0)
        with patch.object(llm, "call", side_effect=self.generated) as model:
            self.refresh(self.now + timedelta(hours=6))
        self.assertEqual([call.args[1] for call in model.call_args_list], ["synthesis", "synthesis_verify"])
        self.assertEqual(self.queue()["state"], "ready")

    def test_budget_block_before_generation_uses_no_attempt_and_no_external_http(self):
        self.prepare()
        with patch.dict(os.environ, {"MODEL_MONTHLY_USD": "0"}), \
                patch("urllib.request.urlopen", side_effect=AssertionError("Budget denial cannot send HTTP")) as http:
            self.refresh()
            http.assert_not_called()
        self.assertEqual(self.queue()["attempts"], 0)
        self.assertGreater(self.queue()["revision"], self.queue()["done_revision"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM model_calls").fetchone()[0], 0)

    def test_invalid_monetary_configuration_uses_no_paid_attempts_before_recovery(self):
        self.prepare()
        invalid = ({"MODEL_MONTHLY_USD": "oops"}, {"MODEL_MONTHLY_USD": "NaN"},
                   {"MODEL_MONTHLY_USD": "Infinity"}, {"MODEL_CALL_RESERVE_USD": "oops"},
                   {"MODEL_CALL_RESERVE_USD": "NaN"}, {"MODEL_CALL_RESERVE_USD": "Infinity"})
        for index, settings in enumerate(invalid):
            with self.subTest(settings=settings), patch.dict(os.environ, settings), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("Invalid configuration cannot send HTTP")) as http:
                self.refresh(self.now + timedelta(hours=index))
                self.assertEqual(self.queue()["attempts"], 0)
                http.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM model_calls").fetchone()[0], 0)
        with patch.object(llm, "call", side_effect=self.generated) as model:
            self.refresh(self.now + timedelta(hours=6))
        self.assertEqual([call.args[1] for call in model.call_args_list], ["synthesis", "synthesis_verify"])
        self.assertEqual(self.queue()["state"], "ready")

    def test_budget_block_after_generation_before_verification_keeps_consumed_attempt(self):
        self.prepare()

        def model(conn, stage, instructions, data):
            if stage == "synthesis_verify":
                raise llm.BudgetExceeded("Synthetic verifier budget denial")
            return self.generated(conn, stage, instructions, data)

        with patch.object(llm, "call", side_effect=model) as calls:
            self.refresh()
        self.assertEqual([call.args[1] for call in calls.call_args_list], ["synthesis", "synthesis_verify"])
        self.assertEqual(self.queue()["attempts"], 1)
        self.assertGreaterEqual(self.queue()["next_retry_at"], self.now + timedelta(minutes=15))
        self.assertEqual(self.current(), [])

    def test_personal_question_facts_never_create_public_claims(self):
        candidate = self.insert(NEGATIVE_CASES[0][1], collected=True, suffix="private")
        question = "Что известно о лазере при сухости влагалища?"
        with patch.object(llm, "call", side_effect=self.model(candidate, application="vaginal", subject=self.topic)):
            worker.process_material(self.conn, self.row(candidate), force=True,
                                    purpose="question", owner_telegram_id=41, question=question)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE active").fetchone()[0], 1)
        self.assertIsNone(self.queue(), "Only private admission must not schedule a public topic")
        self.mark()
        with patch.object(llm, "call", side_effect=AssertionError("Private-only facts cannot enter public synthesis")) as model:
            self.refresh()
            model.assert_not_called()
        self.assertFalse(any(row[1].get("claims") for row in self.current()))

    def test_answer_with_new_material_returns_before_any_common_synthesis(self):
        candidate = self.insert(collected=True)
        processing = self.model(candidate)

        def model(conn, stage, instructions, data):
            if stage == "search_plan":
                return {"subject_identified": True, "query": self.topic, "terms": ["facial", "pigmentation"], "clarification": ""}
            if stage == "answer":
                return {"claims": [{"text": "Synthetic answer", "fact_ids": [f["id"]]} for f in data["facts"]],
                        "limitations": [], "contradictions": [], "missing_data": []}
            if stage == "answer_verify":
                return {"valid": True}
            return processing(conn, stage, instructions, data)

        collection = {"material_ids": [candidate["id"]], "failed_sources": [],
                      "checks": [{"name": "Synthetic source", "planned": True, "complete": True, "status": "success"}]}
        with patch.object(collector, "search", return_value=collection), \
                patch.object(llm, "call", side_effect=model) as calls:
            result = research.answer(self.conn, self.topic, owner_telegram_id=41)
        self.assertTrue(result["claims"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(c.args[1].startswith("synthesis") for c in calls.call_args_list))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM answers").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE active").fetchone()[0], 1)
        self.assertEqual(self.current(), [])

    def test_only_private_allowlisted_owner_sees_safe_background_failure_reason(self):
        self.prepare()
        marker = "UNTRUSTED_BACKGROUND_ERROR_SENTINEL"
        with patch.object(llm, "call", side_effect=RuntimeError(marker)):
            self.refresh()
        self.conn.execute("UPDATE topic_refresh SET last_error=%s WHERE topic=%s",
                          (db.jsonb({"code": "processing_error", "reason": marker}), self.topic))
        with patch.dict(os.environ, {"OWNER_TELEGRAM_ID": "41", "TELEGRAM_CHAT_IDS": "41,42"}), \
                patch.object(llm, "call", side_effect=AssertionError("Status is read-only")):
            owner = bot.command_response(self.conn, 41, "/status")
            other = bot.command_response(self.conn, 42, "/status")
            group = bot.command_response(self.conn, 41, "/status", "group")
        self.assertIn(self.topic, owner)
        self.assertIn(ERROR_REASONS["processing_error"], owner)
        self.assertIn("попыток 1", owner)
        for response in (owner, other, group):
            self.assertNotIn(marker, response)
        self.assertNotIn(self.topic, other)
        self.assertNotIn(self.topic, group)
        for settings in ({"OWNER_TELEGRAM_ID": "", "TELEGRAM_CHAT_IDS": "41"},
                         {"OWNER_TELEGRAM_ID": "41", "TELEGRAM_CHAT_IDS": "42"}):
            with self.subTest(settings=settings), patch.dict(os.environ, settings):
                self.assertNotIn(self.topic, bot.command_response(self.conn, 41, "/status"))


if __name__ == "__main__":
    unittest.main()
