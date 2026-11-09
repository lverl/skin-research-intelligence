"""FIX-008: question retrieval uses real current admission, owner scope and SQL."""
import copy
import os
import unittest
from unittest.mock import patch

import db
import llm
import relevance
import research
from collector import main as collector
from worker import main as worker
import test_reader_processing as processing_fixture
from test_answer_grounding import approval
from test_reader_relevance import NEGATIVE_CASES, POSITIVE_CASES, complete_review, material


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class ReaderAnswersPostgresTests(unittest.TestCase):
    setUp = processing_fixture.ReaderProcessingPostgresTests.setUp
    tearDown = processing_fixture.ReaderProcessingPostgresTests.tearDown
    insert = processing_fixture.ReaderProcessingPostgresTests.insert
    row = processing_fixture.ReaderProcessingPostgresTests.row

    def seed(self, candidate=None, *, count=1, public=True, owner=None, question=None, suffix=""):
        candidate = self.insert(candidate, suffix=suffix)
        self.conn.execute("UPDATE materials SET status='done' WHERE id=%s", (candidate["id"],))
        self.conn.execute("""INSERT INTO findings(material_id,summary,model,prompt_version,topics,kind,verified)
            VALUES (%s,'Synthetic fixture','offline','test',%s,'research',TRUE)""",
            (candidate["id"], db.jsonb([candidate["title"]])))
        ids = []
        for i in range(count):
            ids.append(self.conn.execute("INSERT INTO facts(material_id,statement,quote) VALUES (%s,%s,%s) RETURNING id",
                (candidate["id"], candidate["title"] + ": synthetic observation " + str(i),
                 complete_review(candidate)["evidence_quote"])).fetchone()[0])
        if public:
            self.admit(candidate)
        if owner is not None:
            self.admit(candidate, owner=owner, question=question)
        return candidate, ids

    def admit(self, candidate, *, owner=None, question=None):
        review = complete_review(candidate)
        if owner is not None and "vaginal" in candidate["title"].lower():
            review["application"] = "vaginal"
        decision = relevance.assess(candidate, purpose="question" if owner is not None else "reader",
                                    owner_telegram_id=owner, question=question, review=review)
        self.assertEqual(decision["status"], "eligible", decision)
        relevance.store_assessment(self.conn, candidate, decision)

    def model(self, conn, stage, instructions, data):
        if stage == "reader_review":
            return complete_review({"text": data["text"]})
        if stage == "search_plan":
            return {"subject_identified": True, "query": "retinol pigmentation", "terms": ["retinol", "pigmentation"],
                    "clarification": ""}
        if stage == "answer":
            return {"claims": [{"text": "Synthetic supplied observation " + str(f["id"]), "fact_ids": [f["id"]]}
                               for f in data["facts"]], "limitations": [], "contradictions": [], "missing_data": []}
        if stage == "answer_verify":
            return approval(len(data["answer"]["claims"]))
        raise AssertionError("Unexpected deep or common model stage: " + stage)

    def test_fact_read_never_promotes_old_verified_or_stale_public_admission(self):
        old, _ = self.seed(public=False, suffix="legacy")
        current, ids = self.seed(suffix="current")
        self.assertEqual({x["id"] for x in research.fact_context(self.conn, "pigmentation")}, set(ids))
        self.conn.execute("UPDATE materials SET text=text||' Updated fixture.' WHERE id=%s", (current["id"],))
        self.assertEqual(research.fact_context(self.conn, "pigmentation"), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE active").fetchone()[0], 2)

    def test_changed_profile_does_not_read_old_decision(self):
        self.seed()
        profile = dict(relevance.load_profile(), version="independent-next-profile")
        with patch.object(relevance, "load_profile", return_value=profile):
            self.assertEqual(research.fact_context(self.conn, "pigmentation"), [])

    def test_private_scope_requires_owner_original_question_and_never_enters_public_synthesis(self):
        question = "что известно про сухость влагалища?"
        candidate, ids = self.seed(NEGATIVE_CASES[0][1], public=False, owner=41, question=question)
        allowed = research.fact_context(self.conn, "vaginal dryness", owner_telegram_id=41, question=question)
        self.assertEqual({f["id"] for f in allowed}, set(ids))
        for kwargs in ({}, {"owner_telegram_id": 42, "question": question},
                       {"owner_telegram_id": 41, "question": "другой вопрос"},
                       {"owner_telegram_id": 41, "question": question, "reader_only": True}):
            self.assertEqual(research.fact_context(self.conn, "vaginal dryness", **kwargs), [])
        self.assertIsNone(relevance.get_current_assessment(self.conn, candidate))

    def test_bilingual_aliases_find_actual_subject_but_retinal_does_not_supply_retinol(self):
        cases = [("retinol", "ретинол"), ("pigmentation", "пигментация"), ("Fotona", "Фотона"),
                 ("radiofrequency", "радиочастотный RF"), ("niacinamide", "ниацинамид"),
                 ("vitamin C", "витамин С")]
        for i, (english, russian) in enumerate(cases):
            item = material("alias-" + str(i), "Adult facial topical " + english,
                            "Synthetic adult women used topical facial " + english + ". Human clinical results are reported.")
            _, ids = self.seed(item)
            self.assertTrue(set(ids).issubset({f["id"] for f in research.fact_context(self.conn, russian)}))
            reverse = material("reverse-alias-" + str(i), "Adult facial topical " + russian,
                               "Synthetic adult women used topical facial " + russian + ". Human clinical results are reported.")
            _, reverse_ids = self.seed(reverse)
            self.assertTrue(set(reverse_ids).issubset({f["id"] for f in research.fact_context(self.conn, english)}))
        retinal = material("retinal", "Topical retinaldehyde for adult facial skincare",
                           "Synthetic adult women applied topical retinaldehyde to facial skin. Human clinical results are reported.")
        _, ids = self.seed(retinal)
        self.assertTrue(set(ids).isdisjoint({f["id"] for f in research.fact_context(self.conn, "ретинол")}))

    def test_generic_words_and_four_unrelated_facts_do_not_suppress_real_search(self):
        irrelevant = copy.deepcopy(POSITIVE_CASES[2][2])
        irrelevant["text"] += " Generic words что помогает от are not an indication."
        self.seed(irrelevant, count=4)
        with patch.object(llm, "call", side_effect=self.model), \
                patch.object(collector, "search", return_value={"material_ids": [], "checks": []}) as search:
            result = research.answer(self.conn, "что помогает от пигментных пятен?", owner_telegram_id=41)
        search.assert_called_once()
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["sources"], [])

    def test_four_matching_public_facts_answer_without_plan_search_processing_or_synthesis(self):
        _, ids = self.seed(count=4)
        with patch.object(llm, "call", side_effect=self.model) as model, \
                patch.object(collector, "search", side_effect=AssertionError("No search when current facts suffice")), \
                patch.object(worker, "process_material", side_effect=AssertionError("No deep processing")):
            result = research.answer(self.conn, "что помогает от пигментных пятен?", owner_telegram_id=41)
        self.assertEqual(result["status"], "completed")
        self.assertEqual({i for c in result["claims"] for i in c["fact_ids"]}, set(ids))
        self.assertEqual([call.args[1] for call in model.call_args_list], ["answer", "answer_verify"])

    def test_legacy_verified_facts_gain_targeted_admission_without_reextracting(self):
        _, ids = self.seed(count=4, public=False)
        with patch.object(llm, "call", side_effect=self.model) as model, \
                patch.object(collector, "search", side_effect=AssertionError("Existing facts suffice after review")):
            result = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        self.assertEqual({i for c in result["claims"] for i in c["fact_ids"]}, set(ids))
        self.assertEqual([call.args[1] for call in model.call_args_list], ["reader_review", "answer", "answer_verify"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts").fetchone()[0], 4)

    def test_unattempted_ingest_pending_can_be_reviewed_for_ownerless_existing_facts(self):
        candidate, ids = self.seed(count=4, public=False)
        relevance.record_candidate(self.conn, candidate["id"])
        pending = relevance.get_current_assessment(self.conn, candidate)
        self.assertEqual(pending["status"], "needs_review")
        self.assertFalse(pending["review_attempted"])
        with patch.object(llm, "call", side_effect=self.model) as model, \
                patch.object(collector, "search", side_effect=AssertionError("Existing verified facts suffice")):
            result = research.answer(self.conn, "пигментация")
        self.assertEqual(result["status"], "completed")
        self.assertEqual({i for c in result["claims"] for i in c["fact_ids"]}, set(ids))
        self.assertEqual([c.args[1] for c in model.call_args_list], ["reader_review", "answer", "answer_verify"])
        self.assertEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "eligible")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts").fetchone()[0], 4)

    def test_legacy_review_cap_eight_is_shared_across_original_and_translated_search(self):
        for i in range(10):
            self.seed(public=False, suffix="legacy-" + str(i))
        def pending(conn, stage, instructions, data):
            if stage == "reader_review":
                return {"relevant": False}
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=pending) as model, \
                patch.object(collector, "search", return_value={"material_ids": [], "checks": []}):
            result = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        stages = [c.args[1] for c in model.call_args_list]
        self.assertEqual(stages.count("reader_review"), 8)
        self.assertEqual(stages.count("search_plan"), 1)
        self.assertFalse(any(stage in stages for stage in ("triage", "extract", "verify", "synthesis")))
        self.assertEqual(result["claims"], [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE active").fetchone()[0], 10)

    def test_first_new_document_with_four_relevant_facts_stops_later_deep_processing(self):
        first = self.insert(collected=True, suffix="first")
        second = self.insert(collected=True, suffix="second")
        def model(conn, stage, instructions, data):
            if stage == "triage":
                return {"relevant": True, "important": True, "kind": "research", "summary": "Synthetic facial pigmentation",
                        "topics": ["pigmentation"]}
            if stage == "extract":
                return {"facts": [{"statement": "Pigmentation synthetic observation " + str(i),
                                    "quote": complete_review(first)["evidence_quote"], "payload": {}} for i in range(4)],
                        "limitations": []}
            if stage == "verify":
                return {"accepted_indices": list(range(4)), "limitations": []}
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=model) as calls, patch.object(collector, "search", return_value={
                "material_ids": [first["id"], second["id"]], "checks": [{"status": "success", "complete": True}]}):
            result = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        self.assertEqual(result["status"], "completed", result)
        stages = [c.args[1] for c in calls.call_args_list]
        self.assertEqual(stages.count("triage"), 1)
        self.assertEqual(stages.count("extract"), 1)
        self.assertEqual(stages.count("verify"), 1)
        self.assertEqual(self.conn.execute("SELECT status,attempts FROM materials WHERE id=%s", (second["id"],)).fetchone(),
                         ("new", 0))

    def test_all_rejected_answer_persists_safe_grounding_error_without_model_text(self):
        self.seed(count=4)
        def reject(conn, stage, instructions, data):
            if stage == "answer_verify":
                return {"claims": [{"index": i, "valid": False, "code": "wrong_population"}
                                   for i in range(len(data["answer"]["claims"]))], "notes_valid": True}
            if stage == "answer":
                result = self.model(conn, stage, instructions, data)
                result["claims"][0]["text"] = "SECRET unsupported clinical advice"
                return result
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=reject):
            result = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        saved = self.conn.execute("SELECT content FROM answers ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(saved, result)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["claims"], [])
        error = next(e for e in result["errors"] if e["code"] == "grounding_rejected")
        self.assertEqual(len(error["claim_checks"]), 4)
        self.assertEqual(len(error["claim_sources"]), 4)
        self.assertEqual({i for c in error["claim_sources"] for i in c["fact_ids"]},
                         {row[0] for row in self.conn.execute("SELECT id FROM facts").fetchall()})
        self.assertEqual({i for c in error["claim_sources"] for i in c["material_ids"]},
                         {row[0] for row in self.conn.execute("SELECT id FROM materials").fetchall()})
        self.assertNotIn("SECRET", repr(saved))

    def test_selective_rejection_is_partial_and_history_contains_only_supported_claims(self):
        self.seed(count=4)
        def selective(conn, stage, instructions, data):
            if stage == "answer_verify":
                verdict = approval(4)
                verdict["claims"][1] = {"index": 1, "valid": False, "code": "unsupported_number"}
                return verdict
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=selective):
            result = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["claims"]), 3)
        self.assertEqual(self.conn.execute("SELECT content->>'status' FROM answers ORDER BY id DESC LIMIT 1").fetchone()[0], "partial")

    def test_legacy_bool_verification_is_saved_as_distinct_format_error(self):
        self.seed(count=4)
        def legacy(conn, stage, instructions, data):
            if stage == "answer_verify":
                return {"valid": True}
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=legacy):
            result = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["claims"], [])
        self.assertEqual([e["code"] for e in result["errors"]], ["verification_format"])
        self.assertEqual(self.conn.execute("SELECT content FROM answers ORDER BY id DESC LIMIT 1").fetchone()[0], result)

    def test_empty_generated_answer_is_saved_as_honest_insufficient_data(self):
        self.seed(count=4)
        raw = {"claims": [], "limitations": ["SECRET unsupported effectiveness statement"],
               "contradictions": [], "missing_data": []}
        with patch.object(llm, "call", return_value=raw) as model, \
                patch.object(collector, "search", side_effect=AssertionError("Current matched facts suffice for answering")):
            result = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        self.assertEqual([c.args[1] for c in model.call_args_list], ["answer"])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["errors"], [])
        self.assertNotIn("SECRET", repr(result))
        self.assertEqual(self.conn.execute("SELECT content FROM answers ORDER BY id DESC LIMIT 1").fetchone()[0], result)

    def test_technical_admission_failure_is_failed_while_uncertain_review_is_not_technical_failure(self):
        candidate, _ = self.seed(count=4, public=False)
        collection = {"material_ids": [], "failed_sources": [],
                      "checks": [{"status": "empty", "planned": True, "complete": True}]}
        def technical(conn, stage, instructions, data):
            if stage == "reader_review":
                raise RuntimeError("SECRET provider response")
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=technical) as model, \
                patch.object(collector, "search", return_value=collection):
            failed = research.answer(self.conn, "пигментация", owner_telegram_id=41)
            repeated = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        self.assertEqual([c.args[1] for c in model.call_args_list].count("reader_review"), 1)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(repeated["status"], "failed")
        self.assertTrue(failed["errors"])
        self.assertTrue(any(item["code"] for item in failed["errors"]))
        self.assertTrue(any(item.get("material_id") == candidate["id"] for item in failed["errors"]))
        self.assertNotIn("SECRET", repr(failed))
        def uncertain(conn, stage, instructions, data):
            if stage == "reader_review":
                return {"relevant": False}
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=uncertain), \
                patch.object(collector, "search", return_value=collection):
            unknown = research.answer(self.conn, "пигментация", owner_telegram_id=42)
        self.assertEqual(unknown["status"], "insufficient_data")
        self.assertEqual(unknown["errors"], [])
        self.assertEqual(unknown["claims"], [])
