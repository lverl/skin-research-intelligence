"""Independent FIX-001 policy boundaries; all study texts below are synthetic.

The twelve examples test the agreed reader scope, not medical effectiveness or
the behaviour of a live model. No network, paid call, or production DB is used.
"""

import copy
import unittest
from unittest.mock import patch

import llm
import relevance


TOPICS = {
    "home_skincare", "aesthetic_procedures", "home_devices_massage", "finished_cosmetics",
}


def material(name, title, text, *, kind="research", role="research_candidate", scope="abstract"):
    return {
        "id": 101, "title": title, "text": text, "kind": kind,
        "source_role": role, "text_scope": scope,
        "url": "https://example.org/synthetic-reader-fixture/" + name,
    }


NEGATIVE_CASES = [
    ("vaginal_laser", material(
        "vaginal-laser", "Fractional CO2 and Er:YAG laser for vaginal dryness",
        "Synthetic fixture: women receiving aromatase inhibitors were enrolled for vaginal dryness. "
        "The laser intervention is gynaecological and the trial has no results yet.")),
    ("neonatal_pig_nutrition", material(
        "pig-nutrition", "Vitamins E and C in neonatal pig parenteral nutrition",
        "Synthetic fixture: neonatal pigs received parenteral nutrition with vitamins E and C. "
        "The outcomes concern liver lipids and intestinal perfusion in animals.")),
    ("retinal_transplant", material(
        "retina", "Retinal cell transplantation and restoration of vision",
        "Synthetic fixture: this review discusses donor retinal cell integration and vision restoration. "
        "The clinical specialty is ophthalmology and the organ is the retina.")),
    ("dental_implants", material(
        "dental", "Obesity, periodontitis and inflammation around dental implants",
        "Synthetic fixture: the review concerns periodontitis, salivary changes and dental implant healing. "
        "The population consists of dental patients with periodontal disease.")),
    ("chronic_wound_vitamins", material(
        "wounds", "Vitamin deficiencies and vitamin therapy in chronic wounds",
        "Synthetic fixture: this review concerns vitamin deficiencies in chronic wound patients. "
        "The clinical endpoints are wound closure and healing of pressure ulcers.")),
    ("retinol_cells_only", material(
        "retinol-cells", "Retinol and dermal fibroblasts in cell culture",
        "Synthetic fixture: dermal fibroblast cell cultures were exposed to retinol in vitro. "
        "This is a laboratory cell experiment without human participants or human results.")),
    ("paediatric_only", material(
        "children", "Topical retinoids for facial acne in children aged 8 to 12",
        "Synthetic fixture: the participants were children aged 8 to 12 years with paediatric acne. "
        "The study reports results only for this paediatric population.")),
]


POSITIVE_CASES = [
    ("topical_face_care", "home_skincare", material(
        "topical-face", "Topical facial cream for adult facial pigmentation",
        "Synthetic fixture: adult women received topical facial cream for facial pigmentation. "
        "This controlled human study reports results for the facial skincare intervention.")),
    ("face_neck_procedure", "aesthetic_procedures", material(
        "face-rf", "Radiofrequency treatment for facial and neck rejuvenation in adults",
        "Synthetic fixture: adult women received radiofrequency treatment of the face and neck. "
        "This controlled clinical study contains human results for the cosmetic procedure.")),
    ("home_device", "home_devices_massage", material(
        "home-led", "Home LED facial mask studied in adult women",
        "Synthetic fixture: a home LED facial mask was studied in adult women. "
        "This human study reports outcomes of the home skincare device.")),
    ("finished_product", "finished_cosmetics", material(
        "moisturizer", "Independent human study of a finished facial moisturizer",
        "Synthetic fixture: independent researchers tested a finished facial moisturizer in adults. "
        "The controlled human study evaluates the complete cosmetic product.")),
    ("reference_no_age", "home_skincare", material(
        "spf-reference", "Professional patient information on facial sunscreen",
        "Synthetic public information for adults explains sunscreen application to the face and neck. "
        "The professional reference gives no numerical participant ages.",
        kind="expert", role="professional_information", scope="excerpt")),
]


def complete_review(candidate, topic="home_skincare"):
    """An untrusted classifier result, deliberately reusable on wrong topics."""
    return {
        "relevant": True, "topic": topic, "subject": "facial skin care",
        "application": "face_neck", "audience": "adults", "human_results": True,
        "evidence_quote": candidate["text"].split(". ")[0] + ".",
        "reason": "The proposed reader use is stated in the supplied text.",
    }


class ReaderRelevancePolicyTests(unittest.TestCase):
    def setUp(self):
        self.paid = patch.object(llm, "call", side_effect=AssertionError("Pure scope policy must not call a model"))
        self.paid.start()
        self.addCleanup(self.paid.stop)

    def assert_decision(self, result, expected):
        self.assertEqual(result["status"], expected)
        for field in ("code", "reason", "profile_version", "text_version", "scope_key"):
            self.assertTrue(result.get(field), field + " must be recorded")

    def test_seven_off_topic_cases_are_excluded_even_with_positive_model_flag(self):
        self.assertEqual(len(NEGATIVE_CASES), 7)
        for name, candidate in NEGATIVE_CASES:
            with self.subTest(case=name):
                result = relevance.assess(candidate, review=complete_review(candidate))
                self.assert_decision(result, "excluded")

    def test_five_relevant_cases_pass_with_explicit_grounded_reader_application(self):
        self.assertEqual(len(POSITIVE_CASES), 5)
        for name, topic, candidate in POSITIVE_CASES:
            with self.subTest(case=name):
                review = complete_review(candidate, topic)
                if name == "reference_no_age":
                    review.pop("human_results")
                result = relevance.assess(candidate, review=review)
                self.assert_decision(result, "eligible")
                self.assertEqual(result["topic"], topic)

    def test_lab_mechanism_accompanying_adult_clinical_results_is_not_excluded(self):
        candidate = dict(POSITIVE_CASES[0][2],
                         title="Facial cream: in vitro mechanism and controlled clinical study in adult women")
        self.assert_decision(relevance.assess(candidate, review=complete_review(candidate)), "eligible")

    def test_mixed_age_study_with_stated_adult_human_results_is_not_excluded(self):
        candidate = dict(POSITIVE_CASES[0][2],
                         title="Topical facial cream for adults and children: adult human clinical results")
        self.assert_decision(relevance.assess(candidate, review=complete_review(candidate)), "eligible")

    def test_negated_human_results_do_not_rescue_pure_laboratory_material(self):
        for phrase in ("without human results", "with no human results"):
            with self.subTest(phrase=phrase):
                candidate = material(
                    "negated-human-results", "Retinol: in vitro facial skincare experiment",
                    "Synthetic fixture: retinol was tested in cell culture " + phrase + ". "
                    "No adult participants were enrolled; only cultured cells were studied.",
                )
                self.assert_decision(relevance.assess(candidate, review=complete_review(candidate)), "excluded")

    def test_human_dermal_fibroblast_study_is_not_a_human_clinical_arm(self):
        candidate = material(
            "human-fibroblast-cells", "An in vitro study of retinol in human dermal fibroblasts",
            "Synthetic fixture: human dermal fibroblast cell cultures were exposed to retinol. "
            "Changes in cultured cell markers were measured.",
        )
        self.assert_decision(relevance.assess(candidate, review=complete_review(candidate)), "excluded")

    def test_supported_monitoring_topics_are_only_the_four_selected(self):
        self.assertEqual({topic for _, topic, _ in POSITIVE_CASES}, TOPICS)
        for topic in ("body_hair", "skin_lifestyle", "nutrition", "dental"):
            with self.subTest(topic=topic):
                candidate = POSITIVE_CASES[0][2]
                self.assertNotEqual(relevance.assess(candidate, review=complete_review(candidate, topic))["status"], "eligible")

    def test_a_boolean_flag_is_not_a_reader_admission(self):
        candidate = POSITIVE_CASES[0][2]
        self.assert_decision(relevance.assess(candidate, review={"relevant": True}), "needs_review")

    def test_a_material_record_cannot_admit_itself_by_done_and_verified_flags(self):
        candidate = dict(POSITIVE_CASES[0][2], status="done", verified=True, relevant=True)
        self.assert_decision(relevance.assess(candidate), "needs_review")

    def test_ambiguous_text_waits_for_review(self):
        candidate = material("ambiguous", "A new technique", "Synthetic report: a new technique was examined.")
        self.assert_decision(relevance.assess(candidate), "needs_review")

    def test_no_text_or_metadata_cannot_become_eligible(self):
        original = POSITIVE_CASES[0][2]
        for change in ({"text": ""}, {"text": "   "}, {"text_scope": "metadata"}):
            with self.subTest(change=change):
                candidate = dict(original, **change)
                self.assertNotEqual(relevance.assess(candidate, review=complete_review(original))["status"], "eligible")

    def test_unmatched_or_too_short_grounding_quote_cannot_admit_material(self):
        candidate = POSITIVE_CASES[0][2]
        for quote in ("not present in the supplied synthetic text", "face", ""):
            with self.subTest(quote=quote):
                review = dict(complete_review(candidate), evidence_quote=quote)
                self.assertNotEqual(relevance.assess(candidate, review=review)["status"], "eligible")

    def test_missing_application_audience_topic_or_reason_cannot_admit_material(self):
        candidate = POSITIVE_CASES[0][2]
        for field in ("application", "audience", "topic", "reason", "evidence_quote"):
            with self.subTest(field=field):
                review = complete_review(candidate)
                review.pop(field)
                self.assertNotEqual(relevance.assess(candidate, review=review)["status"], "eligible")

    def test_non_facial_application_does_not_enter_reader_profile(self):
        candidate = POSITIVE_CASES[0][2]
        for application in ("vaginal", "retina", "teeth", "whole_body"):
            with self.subTest(application=application):
                review = dict(complete_review(candidate), application=application)
                self.assertNotEqual(relevance.assess(candidate, review=review)["status"], "eligible")

    def test_a_child_only_review_cannot_admit_an_adult_reader_material(self):
        candidate = POSITIVE_CASES[0][2]
        review = dict(complete_review(candidate), audience="children")
        self.assertNotEqual(relevance.assess(candidate, review=review)["status"], "eligible")

    def test_research_requires_human_results_but_not_a_numerical_age(self):
        candidate = POSITIVE_CASES[0][2]
        for value in (None, False, "true", 1):
            with self.subTest(human_results=value):
                review = dict(complete_review(candidate), human_results=value)
                self.assertNotEqual(relevance.assess(candidate, review=review)["status"], "eligible")
        self.assertEqual(relevance.assess(candidate, review=complete_review(candidate))["status"], "eligible")

    def test_reader_and_owner_question_decisions_have_different_scope_keys(self):
        candidate = POSITIVE_CASES[0][2]
        review = complete_review(candidate)
        reader = relevance.assess(candidate, review=review)
        first = relevance.assess(candidate, purpose="question", owner_telegram_id=41, question="Что известно о ретиноле?", review=review)
        second = relevance.assess(candidate, purpose="question", owner_telegram_id=42, question="Что известно о ретиноле?", review=review)
        other_question = relevance.assess(candidate, purpose="question", owner_telegram_id=41, question="Что известно о витамине C?", review=review)
        self.assertEqual(len({r["scope_key"] for r in (reader, first, second, other_question)}), 4)

    def test_question_scope_normalizes_case_and_whitespace(self):
        candidate = POSITIVE_CASES[0][2]
        first = relevance.assess(candidate, purpose="question", owner_telegram_id=41, question="  Что   известно о РЕТИНОЛЕ?  ")
        second = relevance.assess(candidate, purpose="question", owner_telegram_id=41, question="что известно о ретиноле?")
        self.assertEqual(first["scope_key"], second["scope_key"])

    def test_question_scope_requires_a_verified_owner_and_nonempty_question(self):
        candidate = POSITIVE_CASES[0][2]
        for owner in (None, 0, -1, True, "41"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                relevance.assess(candidate, purpose="question", owner_telegram_id=owner, question="ретинол")
        for question in (None, "", "   "):
            with self.subTest(question=question), self.assertRaises(ValueError):
                relevance.assess(candidate, purpose="question", owner_telegram_id=41, question=question)

    def test_a_changed_profile_or_text_version_is_a_new_decision(self):
        candidate = POSITIVE_CASES[0][2]
        original = relevance.assess(candidate)
        profile = copy.deepcopy(relevance.load_profile())
        profile["version"] = str(profile["version"]) + "-new-fixture-version"
        self.assertNotEqual(relevance.assess(candidate, profile=profile)["profile_version"], original["profile_version"])
        for field, value in (("title", candidate["title"] + " — updated"),
                             ("text", candidate["text"] + " An additional fixture sentence."),
                             ("text_scope", "full_text"), ("source_role", "reference")):
            with self.subTest(field=field):
                changed = relevance.assess(dict(candidate, **{field: value}))
                self.assertNotEqual(changed["text_version"], original["text_version"])

    def test_policy_does_not_mutate_material_or_review(self):
        candidate = copy.deepcopy(POSITIVE_CASES[0][2])
        review = complete_review(candidate)
        before = copy.deepcopy((candidate, review))
        relevance.assess(candidate, review=review)
        self.assertEqual((candidate, review), before)


if __name__ == "__main__":
    unittest.main()
