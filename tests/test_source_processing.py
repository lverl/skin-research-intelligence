"""FIX-005: verified facts survive partial processing, without live calls."""

import os
import unittest
from datetime import datetime, timezone
from contextlib import nullcontext
from unittest.mock import patch
from uuid import uuid4

import llm
import research
from collector import main as collector
from collector import exa
from worker import main as worker
from test_reader_relevance import complete_review


QUOTE = "Twelve fixture observations were recorded."
READABLE_TEXT = "Synthetic human study of topical retinol for adult facial skincare with human results. " + QUOTE


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class AnswerBoundaryDB:
    """Only supplies candidate rows and records writes; it does not implement SQL selection."""
    def __init__(self, candidates=()):
        self.candidates = list(candidates)
        self.saved = []

    def execute(self, sql, args=()):
        if "FROM materials" in sql and "text_scope" in sql:
            return Result(self.candidates)
        if "INSERT INTO answers" in sql:
            self.saved.append(args)
        return Result()


class NoTextBoundaryDB:
    """Supplies a single material record to exercise the public worker boundary."""
    def __init__(self, text, scope, upgrade_before_lock=False):
        self.record = (1, "Retinol fixture", text, scope)
        self.upgrade_before_lock = upgrade_before_lock
        self.status = "new"
        self.facts_active = True

    def transaction(self):
        return nullcontext()

    def execute(self, sql, args=()):
        if "pg_try_advisory_lock" in sql:
            return Result([(True,)])
        if "FROM materials" in sql and "FOR UPDATE" in sql:
            if self.upgrade_before_lock:
                self.record = (1, "Retinol richer fixture", QUOTE, "excerpt")
            return Result([(self.record[2], self.record[3])])
        if "FROM materials" in sql and "SELECT" in sql:
            row = self.record + (("reference",) if "source_role" in sql else ())
            return Result([row])
        if "UPDATE materials" in sql and "status='no_text'" in sql:
            self.status = "no_text"
        if "UPDATE facts" in sql and "active=FALSE" in sql:
            self.facts_active = False
        return Result()


def fact_fixture(material_id=11, fact_id=101):
    return {"id": fact_id, "statement": "Retinol fixture observations", "quote": QUOTE,
            "payload": {}, "material_id": material_id, "title": "Retinol fixture",
            "url": "https://example.org/retinol/" + str(material_id), "scope": "excerpt",
            "kind": "expert", "source_role": "reference"}


def model_fixture(conn, stage, instructions, data):
    if stage == "reader_review":
        return complete_review({"text": data["text"]})
    if stage == "search_plan":
        return {"query": "retinol fixture", "terms": ["retinol"], "clarification": "", "subject_identified": True}
    if stage == "triage":
        return {"relevant": True, "important": True, "kind": "research", "summary": "Контрольный материал",
                "topics": ["retinol fixture"]}
    if stage == "extract":
        return {"facts": [{"statement": "Retinol fixture: twelve observations", "quote": QUOTE, "payload": {}}], "limitations": []}
    if stage == "verify":
        return {"accepted_indices": [0], "limitations": []}
    if stage in {"answer", "synthesis"}:
        return {"claims": [{"text": "Проверенный результат фикстуры", "fact_ids": [data["facts"][0]["id"]]}],
                "limitations": [], "contradictions": [], "missing_data": []}
    if stage in {"answer_verify", "synthesis_verify"}:
        return {"valid": True}
    raise AssertionError("Unexpected model stage: " + stage)


class ProcessingBoundaryTests(unittest.TestCase):
    def test_metadata_and_empty_text_never_call_any_model_stage(self):
        for text, scope in (("Only bibliographic metadata", "metadata"), ("", "excerpt")):
            with self.subTest(scope=scope), patch.object(llm, "call") as model:
                conn = NoTextBoundaryDB(text, scope)
                report = worker.process_material(conn, conn.record, force=True)
            model.assert_not_called()
            self.assertEqual(report["status"], "no_text")
            self.assertEqual(report["facts_saved"], 0)

    def test_concurrent_richer_import_is_not_marked_no_text_or_retired(self):
        conn = NoTextBoundaryDB("Only bibliographic metadata", "metadata", upgrade_before_lock=True)
        with patch.object(llm, "call") as model:
            with self.assertRaises(ValueError):
                worker.process_material(conn, conn.record, force=True)
        model.assert_not_called()
        self.assertEqual(conn.status, "new")
        self.assertTrue(conn.facts_active)

    def test_one_material_failure_keeps_positive_answer_and_original_source(self):
        conn = AnswerBoundaryDB([(11, "Retinol good", QUOTE, "excerpt"), (12, "Retinol failed", QUOTE, "excerpt")])
        stored = []

        def process(conn, row, force=False):
            if row[0] == 11:
                stored.append(fact_fixture())
                return {"status": "processed", "facts_saved": 1, "synthesis_errors": []}
            raise ValueError("Synthetic material failed")

        with patch.object(research, "fact_context", side_effect=lambda *args: list(stored)) as reread, \
                patch.object(collector, "search", return_value={"material_ids": [11, 12], "failed_sources": []}), \
                patch.object(worker, "process_material", side_effect=process), \
                patch.object(llm, "call", side_effect=model_fixture), patch.object(research, "Jsonb", side_effect=lambda value: value):
            result = research.answer(conn, "Что известно о ретиноле?", owner_telegram_id=42)
        self.assertTrue(result["claims"])
        self.assertEqual(result["sources"][0]["material_id"], 11)
        self.assertEqual(result["sources"][0]["url"], fact_fixture()["url"])
        self.assertGreaterEqual(reread.call_count, 2)
        self.assertEqual(len(conn.saved), 1)

    def test_saved_fact_is_reread_even_when_processing_raises_after_saving(self):
        conn = AnswerBoundaryDB([(11, "Retinol fixture", QUOTE, "excerpt")])
        stored = []

        def process(*args, **kwargs):
            stored.append(fact_fixture())
            raise RuntimeError("Synthetic post-save synthesis failure")

        with patch.object(research, "fact_context", side_effect=lambda *args: list(stored)) as reread, \
                patch.object(collector, "search", return_value={"material_ids": [11], "failed_sources": []}), \
                patch.object(worker, "process_material", side_effect=process), \
                patch.object(llm, "call", side_effect=model_fixture), patch.object(research, "Jsonb", side_effect=lambda value: value):
            result = research.answer(conn, "retinol", owner_telegram_id=42)
        self.assertTrue(result["claims"])
        self.assertEqual(result["sources"][0]["id"], 101)
        self.assertGreaterEqual(reread.call_count, 2)

    def test_prior_verified_fact_survives_search_failure(self):
        conn = AnswerBoundaryDB()
        with patch.object(research, "fact_context", return_value=[fact_fixture()]), \
                patch.object(collector, "search", side_effect=TimeoutError("Synthetic source failure")), \
                patch.object(llm, "call", side_effect=model_fixture), patch.object(research, "Jsonb", side_effect=lambda value: value):
            result = research.answer(conn, "retinol", owner_telegram_id=42)
        self.assertTrue(result["claims"])
        self.assertEqual(result["sources"][0]["id"], 101)

    def test_scope_uses_original_question_and_owner_not_planner_synonyms(self):
        conn = AnswerBoundaryDB()
        question = "Что известно о ретиноле?"
        plans = [{"query": query, "terms": ["retinol"], "clarification": ""} for query in (
            "retinol skin review", "vitamin A topical evidence", "different search wording")]
        with patch.object(research, "fact_context", return_value=[]), \
                patch.object(collector, "search", return_value={"material_ids": [], "failed_sources": []}) as search, \
                patch.object(llm, "call", side_effect=plans), patch.object(research, "Jsonb", side_effect=lambda value: value):
            research.answer(conn, question, owner_telegram_id=42)
            research.answer(conn, question, owner_telegram_id=42)
            research.answer(conn, question, owner_telegram_id=43)
        calls = search.call_args_list
        self.assertEqual(calls[0].kwargs["scope_id"], exa.question_scope(question, 42))
        self.assertEqual(calls[1].kwargs["scope_id"], calls[0].kwargs["scope_id"])
        self.assertNotEqual(calls[2].kwargs["scope_id"], calls[0].kwargs["scope_id"])
        self.assertEqual(calls[0].kwargs["owner_telegram_id"], 42)
        self.assertNotEqual(calls[0].args[1], question)

    def test_question_scope_is_captured_before_planner_crosses_month_boundary(self):
        conn = AnswerBoundaryDB()
        question = "retinol"
        clock = [datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)]

        class Clock:
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        def planner(*args, **kwargs):
            clock[0] = datetime(2026, 12, 1, 0, 1, tzinfo=timezone.utc)
            return {"query": "vitamin A", "terms": ["retinol"], "clarification": ""}

        with patch.object(exa, "datetime", Clock), patch.object(research, "fact_context", return_value=[]), \
                patch.object(collector, "search", return_value={"material_ids": [], "failed_sources": []}) as search, \
                patch.object(llm, "call", side_effect=planner), patch.object(research, "Jsonb", side_effect=lambda value: value):
            original_scope = exa.question_scope(question, 42)
            research.answer(conn, question, owner_telegram_id=42)
            later_scope = exa.question_scope(question, 42)
        self.assertNotEqual(original_scope, later_scope)
        self.assertEqual(search.call_args.kwargs["scope_id"], original_scope)

    def test_recognized_broad_subject_does_not_require_a_narrow_goal(self):
        for question in ("Что известно о ретиноле?", "Насколько Фотона эффективна?"):
            plan = {"query": "retinol or Fotona", "terms": ["retinol"], "subject_identified": True,
                    "clarification": "Для какой именно задачи?"}
            with self.subTest(question=question), patch.object(research, "fact_context", return_value=[]), \
                    patch.object(collector, "search", return_value={"material_ids": [], "failed_sources": []}) as search, \
                    patch.object(llm, "call", return_value=plan), patch.object(research, "Jsonb", side_effect=lambda value: value):
                result = research.answer(AnswerBoundaryDB(), question, owner_telegram_id=42)
            search.assert_called_once()
            self.assertFalse(result.get("clarification"))

    def test_unidentified_object_can_request_necessary_clarification(self):
        plan = {"query": "", "terms": [], "subject_identified": False,
                "clarification": "Как называется крем?"}
        with patch.object(research, "fact_context", return_value=[]), patch.object(collector, "search") as search, \
                patch.object(llm, "call", return_value=plan):
            result = research.answer(AnswerBoundaryDB(), "Эффективен ли этот крем?", owner_telegram_id=42)
        self.assertEqual(result["clarification"], "Как называется крем?")
        search.assert_not_called()

    def test_legacy_collector_without_material_ids_keeps_candidate_fallback(self):
        conn = AnswerBoundaryDB([(11, "Retinol fixture", QUOTE, "excerpt")])
        with patch.object(research, "fact_context", return_value=[]), \
                patch.object(collector, "search", return_value={"processed": 1, "failed_sources": []}), \
                patch.object(worker, "process_material", return_value={"status": "processed", "facts_saved": 0, "synthesis_errors": []}) as process, \
                patch.object(llm, "call", side_effect=model_fixture), patch.object(research, "Jsonb", side_effect=lambda value: value):
            research.answer(conn, "retinol", owner_telegram_id=42)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args.args[1][0], 11)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class ProcessingPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        import db
        self.schema = "fix005_" + uuid4().hex
        self.admin = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                    options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=3000")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.close()

    def material(self, *, suffix=None, role="research_candidate", text=READABLE_TEXT, scope="excerpt"):
        return collector.ingest(self.conn, {"url": "https://example.org/retinol/" + (suffix or uuid4().hex),
                                "title": "Adult facial retinol fixture", "text": text, "text_scope": scope, "source_role": role})

    def row(self, mid):
        return self.conn.execute("SELECT id,title,text,text_scope FROM materials WHERE id=%s", (mid,)).fetchone()

    def test_answer_uses_actual_committed_fact_after_post_save_synthesis_failure(self):
        mid = self.material()
        with patch.object(collector, "search", return_value={"material_ids": [mid], "failed_sources": []}), \
                patch.object(research, "synthesize", side_effect=RuntimeError("Synthetic synthesis failure")), \
                patch.object(llm, "call", side_effect=model_fixture):
            result = research.answer(self.conn, "retinol", owner_telegram_id=42)
        self.assertTrue(result["claims"])
        self.assertEqual(result["sources"][0]["material_id"], mid)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE material_id=%s AND active", (mid,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM materials WHERE id=%s", (mid,)).fetchone()[0], "done")

    def test_worker_reports_saved_fact_and_isolates_each_synthesis_topic(self):
        mid = self.material()

        def model(conn, stage, instructions, data):
            if stage == "triage":
                return {**model_fixture(conn, stage, instructions, data), "topics": ["retinol A", "retinol B"]}
            return model_fixture(conn, stage, instructions, data)

        with patch.object(llm, "call", side_effect=model), \
                patch.object(research, "synthesize", side_effect=[RuntimeError("Synthetic topic failure"), None]) as synthesize:
            report = worker.process_material(self.conn, self.row(mid), force=True)
        self.assertEqual(report["status"], "processed")
        self.assertEqual(report["facts_saved"], 1)
        self.assertEqual(len(report["synthesis_errors"]), 1)
        self.assertEqual(synthesize.call_count, 2)
        self.assertEqual(self.conn.execute("SELECT status FROM materials WHERE id=%s", (mid,)).fetchone()[0], "done")

    def test_invalid_quote_cannot_publish_a_verified_fact(self):
        mid = self.material()

        def model(conn, stage, instructions, data):
            if stage == "extract":
                return {"facts": [{"statement": "Invented result", "quote": "This fabricated quote is absent from the page.", "payload": {}}], "limitations": []}
            return model_fixture(conn, stage, instructions, data)

        with patch.object(llm, "call", side_effect=model):
            with self.assertRaises(ValueError):
                worker.process_material(self.conn, self.row(mid), force=True)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE material_id=%s AND active", (mid,)).fetchone()[0], 0)

    def test_metadata_retires_stale_facts_without_model_then_richer_import_requeues(self):
        mid = self.material(text="Only citation metadata", scope="metadata", role="reference")
        self.conn.execute("INSERT INTO facts(material_id,statement,quote) VALUES (%s,'Old unsupported fact',%s)", (mid, QUOTE))
        with patch.object(llm, "call") as model:
            report = worker.process_material(self.conn, self.row(mid), force=True)
        model.assert_not_called()
        self.assertEqual(report["status"], "no_text")
        self.assertEqual(self.conn.execute("SELECT status FROM materials WHERE id=%s", (mid,)).fetchone()[0], "no_text")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE material_id=%s AND active", (mid,)).fetchone()[0], 0)
        url = self.conn.execute("SELECT url FROM materials WHERE id=%s", (mid,)).fetchone()[0]
        upgraded = collector.ingest(self.conn, {"url": url, "title": "Retinol readable", "text": QUOTE,
                                                "text_scope": "excerpt", "source_role": "reference"})
        self.assertEqual(upgraded, mid)
        self.assertEqual(self.conn.execute("SELECT status FROM materials WHERE id=%s", (mid,)).fetchone()[0], "new")

    def test_source_role_limits_unreliable_triage_kind_and_is_retained_in_answer(self):
        cases = (("reference", "research", "expert"),
                 ("professional_information", "guideline", "expert"),
                 ("systematic_review_summary", "research", "review"))
        for role, forged_kind, expected_kind in cases:
            with self.subTest(role=role):
                mid = self.material(role=role)

                def model(conn, stage, instructions, data):
                    if stage in {"triage", "extract", "verify"}:
                        self.assertEqual(data["source_role"], role)
                    if stage == "triage":
                        return {**model_fixture(conn, stage, instructions, data), "kind": forged_kind, "topics": []}
                    return model_fixture(conn, stage, instructions, data)

                with patch.object(llm, "call", side_effect=model):
                    worker.process_material(self.conn, self.row(mid), force=True)
                    facts = [fact for fact in research.fact_context(self.conn, "retinol") if fact["material_id"] == mid]
                    result = research.grounded(self.conn, "answer", "retinol", facts)
                self.assertEqual(self.conn.execute("SELECT kind FROM findings WHERE material_id=%s", (mid,)).fetchone()[0], expected_kind)
                self.assertEqual(result["sources"][0]["source_role"], role)
                self.assertEqual(result["sources"][0]["kind"], expected_kind)

    def test_only_returned_material_ids_are_processed_at_most_eight_prior_facts_survive(self):
        old = self.material()
        with patch.object(llm, "call", side_effect=model_fixture):
            worker.process_material(self.conn, self.row(old), force=True)
        selected = [self.material() for _ in range(10)]
        unrelated = self.material()
        with patch.object(collector, "search", return_value={"material_ids": selected, "failed_sources": []}), \
                patch.object(worker, "process_material", return_value={"status": "processed", "facts_saved": 0, "synthesis_errors": []}) as process, \
                patch.object(llm, "call", side_effect=model_fixture):
            result = research.answer(self.conn, "retinol", owner_telegram_id=42)
        processed = [call.args[1][0] for call in process.call_args_list]
        self.assertEqual(len(processed), 8)
        self.assertTrue(set(processed).issubset(selected))
        self.assertNotIn(unrelated, processed)
        self.assertTrue(result["claims"])
        self.assertEqual(result["sources"][0]["material_id"], old)

    def test_old_patient_finding_cannot_remain_a_guideline_in_a_new_answer(self):
        mid = self.material(role="professional_information")
        self.conn.execute("UPDATE materials SET status='done' WHERE id=%s", (mid,))
        self.conn.execute("""INSERT INTO findings(material_id,summary,model,prompt_version,kind,verified)
                             VALUES (%s,'Legacy fixture','fixture','fixture','guideline',TRUE)""", (mid,))
        self.conn.execute("INSERT INTO facts(material_id,statement,quote) VALUES (%s,'Retinol fixture fact',%s)", (mid, QUOTE))
        with patch.object(llm, "call", side_effect=model_fixture):
            result = research.grounded(self.conn, "answer", "retinol", research.fact_context(self.conn, "retinol"))
        self.assertEqual(result["sources"][0]["kind"], "expert")
        self.assertEqual(result["sources"][0]["source_role"], "professional_information")

    def test_explicit_empty_material_ids_never_fall_back_to_unrelated_queue(self):
        self.material()
        with patch.object(collector, "search", return_value={"material_ids": [], "failed_sources": []}), \
                patch.object(worker, "process_material") as process, patch.object(llm, "call", side_effect=model_fixture):
            result = research.answer(self.conn, "retinol", owner_telegram_id=42)
        process.assert_not_called()
        self.assertEqual(result["claims"], [])

    def answer_with_original_russian_fact_and_expanded_search(self, retire_prior=False):
        old = self.material(role="reference")
        self.conn.execute("UPDATE materials SET title='Контрольный материал о ретиноле' WHERE id=%s", (old,))

        def model(conn, stage, instructions, data):
            if stage == "triage":
                return {**model_fixture(conn, stage, instructions, data), "topics": []}
            if stage == "extract":
                statement = "Ретиноле: двенадцать наблюдений" if "ретиноле" in data["title"] else "alpha: twelve fixture observations"
                return {"facts": [{"statement": statement, "quote": QUOTE, "payload": {}}], "limitations": []}
            if stage == "search_plan":
                return {"query": "alpha evidence", "terms": "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split(),
                        "clarification": "", "subject_identified": True}
            if stage == "answer":
                return {"claims": [{"text": "Факт " + str(fact["id"]), "fact_ids": [fact["id"]]} for fact in data["facts"]],
                        "limitations": [], "contradictions": [], "missing_data": []}
            return model_fixture(conn, stage, instructions, data)

        with patch.object(llm, "call", side_effect=model):
            worker.process_material(self.conn, self.row(old), force=True)
            new = self.material()

            def search(*args, **kwargs):
                if retire_prior:
                    self.conn.execute("UPDATE facts SET active=FALSE WHERE material_id=%s", (old,))
                return {"material_ids": [new], "failed_sources": []}

            with patch.object(collector, "search", side_effect=search):
                result = research.answer(self.conn, "Что известно о ретиноле?", owner_telegram_id=42)
        return old, new, result

    def test_original_russian_facts_and_new_english_facts_both_survive_long_planner_terms(self):
        old, new, result = self.answer_with_original_russian_fact_and_expanded_search()
        self.assertEqual({source["material_id"] for source in result["sources"]}, {old, new})
        self.assertEqual(len(result["claims"]), 2)

    def test_final_reread_does_not_revive_initial_fact_retired_during_search(self):
        old, new, result = self.answer_with_original_russian_fact_and_expanded_search(retire_prior=True)
        self.assertEqual({source["material_id"] for source in result["sources"]}, {new})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE material_id=%s AND active", (old,)).fetchone()[0], 0)

    def test_worker_failure_after_richer_import_does_not_mark_new_content_failed(self):
        mid = self.material(text="Only citation metadata", scope="metadata")
        url = self.conn.execute("SELECT url FROM materials WHERE id=%s", (mid,)).fetchone()[0]

        def concurrent_upgrade(*args, **kwargs):
            collector.ingest(self.conn, {"url": url, "title": "Retinol upgraded", "text": QUOTE, "text_scope": "excerpt"})
            raise ValueError("Текст изменился во время обработки")

        with patch.object(llm, "configuration_issues", return_value=[]), patch.object(llm, "call") as model, \
                patch.object(worker, "process_material", side_effect=concurrent_upgrade):
            worker.process_new(self.conn)
        model.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT status,text,text_scope FROM materials WHERE id=%s", (mid,)).fetchone(),
                         ("new", QUOTE, "excerpt"))


if __name__ == "__main__":
    unittest.main()
