"""DIG-002: editorial items and independent verdicts are untrusted inputs."""
import copy
import unittest

import digest
import relevance
from bot.main import format_digest
from net import SourceError
from test_reader_relevance import material, complete_review


CANDIDATES = [{"id": 1, "facts": [{"id": 11}]}, {"id": 2, "facts": [{"id": 22}]}]
SAFETY_MATERIAL = material("official-cosmetic-recall", "Official recall of a finished facial cream",
    "Synthetic official notice concerns a finished facial cream used by adults. The notice describes a cosmetic product recall.",
    kind="regulatory", role="official_safety", scope="excerpt")
SAFETY_MATERIAL["url"] = "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/synthetic-facial-cream-fixture"


def item(mid=1, fid=11):
    return {"material_ids": [mid], "fact_ids": [fid], "title": "Находка об уходе",
            "summary": "Источник сообщает результат для ухода за кожей.", "limitations": "Доступна аннотация."}


class ReaderFindingValidationTests(unittest.TestCase):
    def test_official_cosmetic_recall_can_be_useful_without_becoming_a_human_trial(self):
        review = dict(complete_review(SAFETY_MATERIAL, "finished_cosmetics"), human_results=False)
        self.assertEqual(relevance.assess(SAFETY_MATERIAL, review=review)["status"], "eligible")
        for url, role in (("https://fda.gov.example.org/safety/recalls-market-withdrawals-safety-alerts/fake", "official_safety"),
                          ("https://www.fda.gov/news-events/press-announcements/synthetic-release", "official_safety"),
                          ("https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/detail.cfm?mdrfoi__id=123", "official_safety"),
                          (SAFETY_MATERIAL["url"], "unknown")):
            with self.subTest(url=url, role=role):
                forged = dict(SAFETY_MATERIAL, url=url, source_role=role)
                self.assertNotEqual(relevance.assess(forged, review=review)["status"], "eligible")
        wrong = dict(review, application="vaginal")
        self.assertEqual(relevance.assess(SAFETY_MATERIAL, review=wrong)["status"], "excluded")

    def test_telegram_shows_finding_headline_and_its_short_explanation(self):
        finding = item()
        rendered = format_digest("2026-11-09", {"items": [finding]})
        self.assertIn(finding["title"], rendered)
        self.assertIn(finding["summary"], rendered)
        self.assertLess(rendered.index(finding["title"]), rendered.index(finding["summary"]))
        self.assertIn("Старый пересказ", format_digest("2026-11-09", {"items": [{"summary": "Старый пересказ"}]}))

    def test_telegram_preserves_source_type_year_scope_and_primary_link(self):
        for kind, role, expected in (("research", "research_candidate", "исследован"),
                                     ("review", "research_candidate", "обзор"),
                                     ("regulatory", "official_safety", "официальное сообщение о безопасности")):
            with self.subTest(kind=kind):
                finding = dict(item(), sources=[{"url": "https://example.org/source", "kind": kind,
                    "source_role": role, "scope": "abstract", "published_at": "2024-06-01T00:00:00Z"}])
                rendered = format_digest("2026-11-09", {"items": [finding]})
                self.assertIn(finding["title"], rendered)
                self.assertIn(finding["summary"], rendered)
                self.assertIn(expected, rendered.lower())
                self.assertIn("2024", rendered)
                self.assertRegex(rendered.lower(), "аннотац")
                self.assertIn("https://example.org/source", rendered)

    def test_valid_item_preserves_only_publishable_fields(self):
        raw = item()
        raw["significance"] = "SECRET unsupported recommendation"
        accepted, indices, checks = digest._split_reader_items({"items": [raw]}, CANDIDATES)
        self.assertEqual(accepted, [item()])
        self.assertEqual(indices, [0])
        self.assertEqual(checks, [])
        self.assertNotIn("SECRET", repr(accepted))

    def test_independent_good_item_survives_bad_references_types_and_language(self):
        bad = [None, dict(item(), material_ids=[True]), dict(item(), fact_ids=[True]),
               dict(item(), material_ids=[1, 2]), dict(item(), fact_ids=[22]),
               dict(item(), title="English-only headline"), dict(item(), summary="English-only summary"),
               dict(item(), title="Я" * 161), dict(item(), summary="Я" * 401),
               dict(item(), limitations="SECRET English-only clinical advice")]
        # Each malformed item is paired with an unrelated valid material so the
        # batch limit cannot mask the individual structural boundary.
        for candidate in bad:
            with self.subTest(candidate=candidate):
                accepted, indices, checks = digest._split_reader_items({"items": [candidate, item(2, 22)]}, CANDIDATES)
                self.assertEqual(accepted, [item(2, 22)])
                self.assertEqual(indices, [1])
                self.assertEqual(checks[0]["index"], 0)
                self.assertFalse(checks[0]["valid"])

    def test_one_material_cannot_be_repeated_to_fill_a_digest(self):
        accepted, indices, checks = digest._split_reader_items({"items": [item(), item()]}, CANDIDATES)
        self.assertEqual(accepted, [item()])
        self.assertEqual(indices, [0])
        self.assertFalse(checks[0]["valid"])

    def test_batch_and_container_shape_are_bounded(self):
        for raw in (None, [], {}, {"items": None}, {"items": [item()] * 11}):
            with self.subTest(raw=raw):
                with self.assertRaises(SourceError):
                    digest._split_reader_items(raw, CANDIDATES)

    def test_per_item_semantic_rejection_is_distinct_and_does_not_reject_neighbor(self):
        for code in ("unsupported", "wrong_subject", "wrong_population", "wrong_protocol", "unsupported_number",
                     "personal_advice", "not_useful", "duplicate"):
            with self.subTest(code=code):
                verdict = digest._verified_reader_items({"items": [
                    {"index": 0, "valid": True, "code": "supported"},
                    {"index": 1, "valid": False, "code": code}]}, 2)
                self.assertTrue(verdict[0]["valid"])
                self.assertFalse(verdict[1]["valid"])

    def test_bool_summary_and_incomplete_or_ill_typed_verdicts_cannot_approve_findings(self):
        okay = {"items": [{"index": 0, "valid": True, "code": "supported"}]}
        bad = [{"valid": True}, {}, None, {"items": []},
               {"items": okay["items"] * 2}]
        for key, value in (("index", True), ("index", "0"), ("index", -1), ("index", 1),
                           ("valid", 1), ("valid", "true"), ("code", []), ("code", {}),
                           ("code", "not_useful"), ("code", "unknown")):
            changed = copy.deepcopy(okay)
            changed["items"][0][key] = value
            bad.append(changed)
        for verdict in bad:
            with self.subTest(verdict=verdict):
                with self.assertRaises(SourceError) as caught:
                    digest._verified_reader_items(verdict, 1)
                self.assertEqual(caught.exception.safe_code, "verification_format")
