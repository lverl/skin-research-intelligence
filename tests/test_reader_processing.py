"""FIX-007: real admission/cache/worker boundaries, with only external models mocked."""

import copy
import json
import os
import unittest
from unittest.mock import patch

import db
import llm
import relevance
import research
from collector import main as collector
from worker import main as worker
import test_relevance_postgres as schema_fixture
from test_reader_relevance import NEGATIVE_CASES, POSITIVE_CASES, complete_review


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class ReaderProcessingPostgresTests(unittest.TestCase):
    def setUp(self):
        schema_fixture.ReaderRelevancePostgresTests.setUp(self)
        self.settings = patch.dict(os.environ, {
            "MODEL": "offline-reader-fixture", "OPENROUTER_API_KEY": "offline-fixture",
            "MODEL_MONTHLY_USD": "10", "MODEL_CALL_RESERVE_USD": "1",
        })
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def tearDown(self):
        schema_fixture.ReaderRelevancePostgresTests.tearDown(self)

    def insert(self, original=None, *, collected=False, suffix=""):
        candidate = copy.deepcopy(original or POSITIVE_CASES[0][2])
        if suffix:
            candidate["title"] += " " + suffix
            candidate["url"] += "-" + suffix
        if collected:
            candidate["id"] = collector.ingest(self.conn, candidate)
        else:
            candidate["id"] = self.conn.execute(
                """INSERT INTO materials(url,title,text,text_scope,source_role,status)
                   VALUES (%s,%s,%s,%s,%s,'new') RETURNING id""",
                (candidate["url"], candidate["title"], candidate["text"],
                 candidate["text_scope"], candidate["source_role"]),
            ).fetchone()[0]
        return candidate

    def row(self, candidate):
        return self.conn.execute(
            "SELECT id,title,text,text_scope FROM materials WHERE id=%s", (candidate["id"],),
        ).fetchone()

    def model(self, candidate, *, application="face_neck", subject="facial pigmentation"):
        quote = complete_review(candidate)["evidence_quote"]

        def respond(conn, stage, instructions, data):
            if stage == "reader_review":
                return dict(complete_review(candidate), application=application, subject=subject)
            if stage == "triage":
                return {"relevant": True, "important": True, "kind": "research",
                        "summary": "Синтетическая находка для проверки обработки",
                        "topics": [subject]}
            if stage == "extract":
                return {"facts": [{"statement": subject + ": synthetic fixture observation",
                                    "quote": quote, "payload": {"fixture": True}}], "limitations": []}
            if stage == "verify":
                return {"accepted_indices": [0], "limitations": []}
            raise AssertionError("Unexpected external stage in FIX-007 fixture: " + stage)

        return respond

    def test_initial_pending_runs_one_review_and_eligible_cache_survives_restart(self):
        import psycopg
        candidate = self.insert(collected=True)
        initial = relevance.get_current_assessment(self.conn, candidate)
        self.assertEqual(initial["status"], "needs_review")
        self.assertFalse(initial.get("review_attempted"))
        with patch.object(llm, "call", return_value=complete_review(candidate)) as model:
            decision = relevance.ensure_assessment(self.conn, candidate)
            self.assertEqual(decision["status"], "eligible")
            self.assertEqual(model.call_count, 1)
            self.assertEqual(model.call_args.args[1], "reader_review")
            self.assertEqual(relevance.ensure_assessment(self.conn, candidate)["status"], "eligible")
            with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                 options=f"-c search_path={self.schema}") as restarted:
                self.assertEqual(relevance.ensure_assessment(restarted, candidate)["status"], "eligible")
            self.assertEqual(model.call_count, 1)

    def test_early_excluded_cache_never_calls_model_even_with_force_processing(self):
        candidate = self.insert(NEGATIVE_CASES[0][1], collected=True)
        with patch.object(llm, "call", side_effect=AssertionError("Excluded reader material cannot consume a model")) as model:
            self.assertEqual(relevance.ensure_assessment(self.conn, candidate)["status"], "excluded")
            report = worker.process_material(self.conn, self.row(candidate), force=True)
            self.assertEqual(report["status"], "excluded")
            self.assertEqual(report["facts_saved"], 0)
            model.assert_not_called()

    def test_attempted_incomplete_review_is_not_paid_again(self):
        candidate = self.insert(collected=True)
        with patch.object(llm, "call", return_value={"relevant": True}) as model:
            first = relevance.ensure_assessment(self.conn, candidate)
            self.assertEqual(first["status"], "needs_review")
            self.assertTrue(first["review_attempted"])
            self.assertEqual(relevance.ensure_assessment(self.conn, candidate)["status"], "needs_review")
            self.assertEqual(model.call_count, 1)

    def test_negative_boolean_without_scope_evidence_waits_instead_of_skipping(self):
        candidate = self.insert(collected=True)
        with patch.object(llm, "call", return_value={"relevant": False}) as model:
            report = worker.process_material(self.conn, self.row(candidate), force=True)
            self.assertEqual(report["status"], "needs_review")
            self.assertEqual(self.conn.execute("SELECT status FROM materials WHERE id=%s", (candidate["id"],)).fetchone()[0], "new")
            cached = relevance.get_current_assessment(self.conn, candidate)
            self.assertEqual(cached["status"], "needs_review")
            self.assertTrue(cached["review_attempted"])
            self.assertEqual(worker.process_material(self.conn, self.row(candidate), force=True)["status"], "needs_review")
            self.assertEqual([call.args[1] for call in model.call_args_list], ["reader_review"])

    def test_allow_model_false_keeps_pending_available_for_a_later_single_review(self):
        candidate = self.insert()
        with patch.object(llm, "call", return_value=complete_review(candidate)) as model:
            pending = relevance.ensure_assessment(self.conn, candidate, allow_model=False)
            self.assertEqual(pending["status"], "needs_review")
            self.assertFalse(pending.get("review_attempted"))
            model.assert_not_called()
            self.assertEqual(relevance.ensure_assessment(self.conn, candidate)["status"], "eligible")
            self.assertEqual(model.call_count, 1)

    def test_metadata_and_blank_text_do_not_call_any_model_stage(self):
        for index, change in enumerate(({"text_scope": "metadata"}, {"text": ""})):
            with self.subTest(change=change):
                original = dict(POSITIVE_CASES[0][2], **change)
                candidate = self.insert(original, suffix=str(index))
                with patch.object(llm, "call", side_effect=AssertionError("No text must be free")) as model:
                    decision = relevance.ensure_assessment(self.conn, candidate)
                    self.assertNotEqual(decision["status"], "eligible")
                    report = worker.process_material(self.conn, self.row(candidate), force=True)
                    self.assertEqual(report["status"], "no_text")
                    self.assertEqual(report["facts_saved"], 0)
                    model.assert_not_called()

    def test_failed_review_is_safe_cached_pending_not_eligible(self):
        candidate = self.insert(collected=True)
        marker = "UNTRUSTED_MODEL_RESPONSE_SENTINEL"
        with patch.object(llm, "call", side_effect=RuntimeError(marker)) as model:
            result = relevance.ensure_assessment(self.conn, candidate)
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["code"], "review_failed")
            self.assertTrue(result["review_attempted"])
            self.assertTrue(result.get("errors"))
            cached = relevance.get_current_assessment(self.conn, candidate)
            self.assertNotIn(marker, json.dumps(cached, ensure_ascii=False))
            self.assertNotEqual(relevance.ensure_assessment(self.conn, candidate)["status"], "eligible")
            self.assertEqual(model.call_count, 1)

    def test_budget_denial_does_not_mark_review_as_attempted_or_block_later_review(self):
        candidate = self.insert(collected=True)
        with patch.object(llm, "call", side_effect=llm.BudgetExceeded("Synthetic budget denial")):
            with self.assertRaises(llm.BudgetExceeded):
                relevance.ensure_assessment(self.conn, candidate)
        cached = relevance.get_current_assessment(self.conn, candidate)
        self.assertNotEqual(cached["status"], "eligible")
        self.assertFalse(cached.get("review_attempted"))
        with patch.object(llm, "call", return_value=complete_review(candidate)) as model:
            self.assertEqual(relevance.ensure_assessment(self.conn, candidate)["status"], "eligible")
            self.assertEqual(model.call_count, 1)

    def test_bad_review_grounding_or_application_never_starts_deep_analysis(self):
        for index, change in enumerate((
            {"evidence_quote": "This fabricated quote does not exist in the candidate"},
            {"application": "vaginal"}, {"relevant": True, "human_results": False},
        )):
            with self.subTest(change=change):
                candidate = self.insert(suffix=str(index))
                response = dict(complete_review(candidate), **change)
                with patch.object(llm, "call", return_value=response) as model:
                    report = worker.process_material(self.conn, self.row(candidate), force=True)
                self.assertIn(report["status"], {"needs_review", "excluded"})
                self.assertEqual([call.args[1] for call in model.call_args_list], ["reader_review"])
                self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE material_id=%s", (candidate["id"],)).fetchone()[0], 0)

    def test_changed_text_and_profile_allow_a_new_review(self):
        candidate = self.insert(collected=True)
        with patch.object(llm, "call", return_value={"relevant": True}) as first:
            relevance.ensure_assessment(self.conn, candidate)
            self.assertEqual(first.call_count, 1)
        candidate["text"] += " An additional synthetic paragraph."
        self.conn.execute("UPDATE materials SET text=%s WHERE id=%s", (candidate["text"], candidate["id"]))
        with patch.object(llm, "call", return_value=complete_review(candidate)) as second:
            self.assertEqual(relevance.ensure_assessment(self.conn, candidate)["status"], "eligible")
            self.assertEqual(second.call_count, 1)
        changed_profile = relevance.load_profile()
        changed_profile["version"] = str(changed_profile["version"]) + "-processing-fixture"
        with patch.object(relevance, "load_profile", return_value=changed_profile), \
                patch.object(llm, "call", return_value=complete_review(candidate)) as third:
            self.assertEqual(relevance.ensure_assessment(self.conn, candidate)["status"], "eligible")
            self.assertEqual(third.call_count, 1)

    def test_positive_forced_material_produces_active_facts_and_current_admission(self):
        candidate = self.insert(collected=True)
        with patch.object(llm, "call", side_effect=self.model(candidate)) as model, \
                patch.object(research, "synthesize"):
            report = worker.process_material(self.conn, self.row(candidate), force=True)
        self.assertEqual(report["status"], "processed")
        self.assertEqual(report["facts_saved"], 1)
        self.assertEqual([call.args[1] for call in model.call_args_list], ["reader_review", "triage", "extract", "verify"])
        stored = self.conn.execute("SELECT statement,quote,active FROM facts WHERE material_id=%s", (candidate["id"],)).fetchall()
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0][2])
        self.assertIn(stored[0][1], candidate["text"])
        self.assertEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "eligible")

    def test_private_outside_profile_processing_does_not_grant_public_admission(self):
        candidate = self.insert(NEGATIVE_CASES[0][1], collected=True)
        question = "Что известно о лазере при сухости влагалища?"
        with patch.object(llm, "call", side_effect=self.model(candidate, application="vaginal", subject="vaginal dryness")), \
                patch.object(research, "synthesize"):
            report = worker.process_material(self.conn, self.row(candidate), force=True,
                                             purpose="question", owner_telegram_id=41, question=question)
        self.assertEqual(report["status"], "processed")
        self.assertEqual(report["facts_saved"], 1)
        self.assertEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "excluded")
        self.assertEqual(relevance.get_current_assessment(
            self.conn, candidate, purpose="question", owner_telegram_id=41, question=question,
        )["status"], "eligible")
        self.assertIsNone(relevance.get_current_assessment(
            self.conn, candidate, purpose="question", owner_telegram_id=42, question=question,
        ))

    def test_legacy_processing_is_topical_and_capped_at_eight_per_cycle(self):
        targets = [self.insert(suffix="legacy-target-" + str(i)) for i in range(12)]
        outsiders = [self.insert(NEGATIVE_CASES[2][1], suffix="unrelated-" + str(i)) for i in range(12)]
        with patch.object(llm, "call", side_effect=self.model(targets[0])) as model, \
                patch.object(research, "synthesize"):
            worker.process_new(self.conn)
        target_ids = [row["id"] for row in targets]
        done = self.conn.execute("SELECT count(*) FROM materials WHERE id=ANY(%s) AND status='done'", (target_ids,)).fetchone()[0]
        self.assertEqual(done, 8)
        self.assertEqual(sum(call.args[1] == "reader_review" for call in model.call_args_list), 8)
        for candidate in outsiders:
            self.assertIsNone(relevance.get_current_assessment(self.conn, candidate))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials").fetchone()[0], 24)

    def test_new_imports_do_not_replace_or_expand_the_eight_legacy_limit(self):
        current = [self.insert(collected=True, suffix="fresh-" + str(i)) for i in range(3)]
        old = [self.insert(suffix="legacy-" + str(i)) for i in range(10)]
        with patch.object(llm, "call", side_effect=self.model(current[0])), \
                patch.object(research, "synthesize"):
            worker.process_new(self.conn)
        current_done = self.conn.execute("SELECT count(*) FROM materials WHERE id=ANY(%s) AND status='done'", ([c["id"] for c in current],)).fetchone()[0]
        old_done = self.conn.execute("SELECT count(*) FROM materials WHERE id=ANY(%s) AND status='done'", ([c["id"] for c in old],)).fetchone()[0]
        self.assertEqual(current_done, 3)
        self.assertEqual(old_done, 8)

    def test_processed_or_reviewed_unchanged_material_is_not_paid_next_cycle(self):
        candidate = self.insert(collected=True)
        with patch.object(llm, "call", side_effect=self.model(candidate)) as model, \
                patch.object(research, "synthesize"):
            worker.process_new(self.conn)
            count = model.call_count
            worker.process_new(self.conn)
            self.assertEqual(model.call_count, count)
        pending_candidate = self.insert(suffix="pending-once")
        with patch.object(llm, "call", return_value={"relevant": True}) as model:
            worker.process_new(self.conn)
            self.assertEqual(model.call_count, 1)
            worker.process_new(self.conn)
            self.assertEqual(model.call_count, 1)
        self.assertTrue(relevance.get_current_assessment(self.conn, pending_candidate)["review_attempted"])


if __name__ == "__main__":
    unittest.main()
