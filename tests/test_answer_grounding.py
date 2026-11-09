"""FIX-008: untrusted generation and per-claim verdicts cross separate boundaries."""
import copy
import unittest
from unittest.mock import patch

import llm
import research
import relevance
from net import SourceError
from test_reader_relevance import NEGATIVE_CASES, complete_review, material


FACTS = [
    {"id": 11, "material_id": 101, "statement": "Synthetic adult facial retinol observation", "quote": "Synthetic source observation one.",
     "title": "Adult facial retinol", "url": "https://example.org/one", "scope": "abstract"},
    {"id": 12, "material_id": 102, "statement": "Synthetic adult facial pigmentation observation", "quote": "Synthetic source observation two.",
     "title": "Adult facial pigmentation", "url": "https://example.org/two", "scope": "excerpt"},
]


def approval(count, *, notes_valid=True):
    return {"claims": [{"index": i, "valid": True, "code": "supported"} for i in range(count)],
            "notes_valid": notes_valid}


class AnswerGroundingTests(unittest.TestCase):
    def generated(self):
        return {"claims": [{"text": "Synthetic observation one", "fact_ids": [11]},
                           {"text": "Synthetic observation two", "fact_ids": [12]}],
                "limitations": ["Only supplied abstracts were available."],
                "contradictions": [], "missing_data": ["No full text was provided."]}

    def run_grounded(self, raw, verdict):
        with patch.object(llm, "call", side_effect=[raw, verdict]) as model:
            result = research.grounded(None, "answer", "retinol and pigmentation", copy.deepcopy(FACTS))
        return result, model

    def test_supported_claims_preserve_sources_and_require_separate_verification(self):
        result, model = self.run_grounded(self.generated(), approval(2))
        self.assertEqual(len(result["claims"]), 2)
        self.assertEqual([x["id"] for x in result["sources"]], [11, 12])
        self.assertFalse(result["grounding_partial"])
        self.assertEqual([c.args[1] for c in model.call_args_list], ["answer", "answer_verify"])
        self.assertEqual(model.call_args.args[3]["answer"]["claims"], result["claims"])

    def test_selective_rejection_keeps_only_independent_supported_claim_and_source(self):
        for reason in ("unsupported", "wrong_subject", "wrong_population", "wrong_protocol",
                       "unsupported_number", "personal_advice"):
            with self.subTest(reason=reason):
                verdict = approval(2)
                verdict["claims"][1] = {"index": 1, "valid": False, "code": reason}
                result, _ = self.run_grounded(self.generated(), verdict)
                self.assertEqual(result["claims"], self.generated()["claims"][:1])
                self.assertEqual([f["id"] for f in result["sources"]], [11])
                self.assertTrue(result["grounding_partial"])
                self.assertEqual(result["claim_checks"][1], {"index": 1, "valid": False, "code": reason})

    def test_structurally_bad_claims_never_reach_verifier_and_indices_remain_original(self):
        raw = self.generated()
        good = raw["claims"][0]
        raw["claims"] = [None, {"text": "Forged", "fact_ids": [999]}, good,
                         {"text": "Boolean ID", "fact_ids": [True]}, {"text": "   ", "fact_ids": [11]},
                         {"text": "String ID", "fact_ids": ["11"]}, {"text": "No ids", "fact_ids": []}]
        result, model = self.run_grounded(raw, approval(1))
        self.assertEqual(model.call_args.args[3]["answer"]["claims"], [good])
        self.assertEqual(result["claims"], [good])
        self.assertEqual([c["index"] for c in result["claim_checks"]], list(range(7)))
        self.assertEqual([c["index"] for c in result["claim_checks"] if c["valid"]], [2])
        self.assertTrue(result["grounding_partial"])

    def test_all_semantic_rejections_expose_only_safe_checks(self):
        raw = self.generated()
        raw["claims"][0]["text"] = "SECRET fabricated clinical text"
        verdict = {"claims": [{"index": i, "valid": False, "code": "wrong_protocol"} for i in range(2)],
                   "notes_valid": True}
        with patch.object(llm, "call", side_effect=[raw, verdict]):
            with self.assertRaises(SourceError) as caught:
                research.grounded(None, "answer", "retinol", FACTS)
        self.assertEqual(caught.exception.safe_code, "grounding_rejected")
        self.assertEqual(caught.exception.claim_checks, verdict["claims"])
        self.assertNotIn("SECRET", repr(caught.exception.claim_checks))
        self.assertEqual(caught.exception.claim_sources,
                         [{"index": 0, "fact_ids": [11], "material_ids": [101]},
                          {"index": 1, "fact_ids": [12], "material_ids": [102]}])
        self.assertNotIn("SECRET", repr(caught.exception.claim_sources))

    def test_all_structural_rejections_do_not_pay_for_verifier(self):
        raw = dict(self.generated(), claims=[{"text": "Forged", "fact_ids": [999]}])
        with patch.object(llm, "call", return_value=raw) as model:
            with self.assertRaises(SourceError) as caught:
                research.grounded(None, "answer", "retinol", FACTS)
        self.assertEqual(caught.exception.safe_code, "grounding_rejected")
        self.assertEqual(model.call_count, 1)
        self.assertFalse(caught.exception.claim_checks[0]["valid"])

    def test_empty_answer_abstains_without_verifier_or_hidden_advice_in_notes(self):
        raw = {"claims": [], "limitations": ["SECRET unverified clinical advice"],
               "contradictions": ["SECRET absence of effect"], "missing_data": ["SECRET prescribe treatment"]}
        with patch.object(llm, "call", return_value=raw) as model:
            result = research.grounded(None, "answer", "retinol", FACTS)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["limitations"], [])
        self.assertEqual(result["contradictions"], [])
        self.assertTrue(result["missing_data"])
        self.assertNotIn("SECRET", repr(result))
        with patch.object(llm, "call", return_value=raw) as model:
            with self.assertRaises(SourceError) as caught:
                research.grounded(None, "synthesis", "retinol", FACTS)
        self.assertEqual(caught.exception.safe_code, "grounding_rejected")
        self.assertEqual(model.call_count, 1)

    def test_unverified_notes_clear_all_model_note_lists_without_hiding_claims(self):
        raw = self.generated()
        for key in ("limitations", "contradictions", "missing_data"):
            raw[key] = ["SECRET clinical advice in " + key]
        result, _ = self.run_grounded(raw, approval(2, notes_valid=False))
        self.assertEqual(len(result["claims"]), 2)
        self.assertTrue(result["grounding_partial"])
        self.assertNotIn("SECRET", repr(result))
        self.assertEqual(result["contradictions"], [])
        self.assertEqual(result["missing_data"], [])
        self.assertTrue(result["limitations"])

    def test_ambiguous_or_incomplete_verifier_is_format_failure_not_approval(self):
        cases = [
            {"valid": True}, None, [], {}, approval(1),
            {"claims": approval(2)["claims"]}, approval(2, notes_valid=1),
            {"claims": [approval(2)["claims"][0]] * 2, "notes_valid": True},
        ]
        for field, value in (("index", True), ("index", "0"), ("index", -1), ("index", 2),
                             ("valid", 1), ("valid", "true"), ("code", "invented"),
                             ("code", "unsupported"), ("code", []), ("code", {})):
            verdict = approval(2)
            verdict["claims"][0][field] = value
            cases.append(verdict)
        verdict = approval(2)
        verdict["claims"][0]["valid"] = False
        cases.append(verdict)
        for verdict in cases:
            with self.subTest(verdict=verdict):
                with patch.object(llm, "call", side_effect=[self.generated(), verdict]):
                    with self.assertRaises(SourceError) as caught:
                        research.grounded(None, "answer", "retinol", FACTS)
                self.assertEqual(caught.exception.safe_code, "verification_format")

    def test_synthesis_uses_same_strict_per_claim_boundary(self):
        with patch.object(llm, "call", side_effect=[self.generated(), {"valid": True}]):
            with self.assertRaises(SourceError) as caught:
                research.grounded(None, "synthesis", "retinol", FACTS)
        self.assertEqual(caught.exception.safe_code, "verification_format")

    def test_four_unrelated_or_duplicate_facts_cannot_satisfy_a_question(self):
        irrelevant = [{"id": i, "title": "LED face mask", "statement": "что известно от home LED", "quote": "LED fixture"}
                      for i in range(4)]
        self.assertFalse(research._facts_sufficient(irrelevant, "что помогает от пигментных пятен?"))
        retinol = [{"id": i, "title": "Retinol adult facial study", "statement": "retinol", "quote": "fixture"}
                   for i in range(4)]
        self.assertTrue(research._facts_sufficient(retinol, "расскажи про ретинол"))
        self.assertFalse(research._facts_sufficient([retinol[0]] * 4, "ретинол"))
        self.assertFalse(research._facts_sufficient(retinol, "ретинол и пигментация"))
        retinol[0]["statement"] += " pigmentation"
        self.assertTrue(research._facts_sufficient(retinol, "ретинол и пигментация"))

    def test_retinal_is_not_a_bilingual_alias_of_retinol(self):
        facts = [{"id": i, "title": "Retinaldehyde adult facial study", "statement": "retinal", "quote": "fixture"}
                 for i in range(4)]
        self.assertFalse(research._facts_sufficient(facts, "ретинол"))

    def test_specific_indication_cannot_be_replaced_by_four_other_protocol_facts(self):
        facts = [{"id": i, "title": "Fotona for facial wrinkles", "statement": "Adult wrinkle observation",
                  "quote": "Synthetic wrinkle source"} for i in range(4)]
        self.assertFalse(research._facts_sufficient(facts, "Fotona при розацеа"))

    def test_topical_retinal_is_reader_eligible_without_becoming_retinol(self):
        candidate = material("retinal-face", "Topical retinal for adult facial skincare",
                             "Synthetic adult women applied topical retinal to facial skin. Human clinical results are reported.")
        self.assertEqual(relevance.assess(candidate, review=complete_review(candidate))["status"], "eligible")

    def test_ordinary_fotona_and_retinol_questions_keep_reader_application_and_lab_bounds(self):
        candidate = material("fotona-face", "Fotona cosmetic facial procedure for adult women",
                             "Synthetic adult women received Fotona facial treatment. Human clinical results are reported.")
        review = dict(complete_review(candidate, "aesthetic_procedures"), application="vaginal")
        decision = relevance.assess(candidate, purpose="question", owner_telegram_id=41,
                                    question="Насколько эффективна Фотона?", review=review)
        self.assertEqual(decision["status"], "excluded")
        lab = NEGATIVE_CASES[5][1]
        decision = relevance.assess(lab, purpose="question", owner_telegram_id=41,
                                    question="расскажи про ретинол", review=complete_review(lab))
        self.assertEqual(decision["status"], "excluded")
        explicit = relevance.assess(lab, purpose="question", owner_telegram_id=41,
                                    question="Как изучали ретинол in vitro в клетках?", review=complete_review(lab))
        self.assertEqual(explicit["status"], "eligible")

