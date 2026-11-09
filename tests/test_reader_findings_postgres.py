"""DIG-002: the same verified reader finding is reused by feed and daily edition."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import db
import digest
import llm
import relevance
from collector import main as collector
import test_reader_answers_postgres as answer_fixture
from test_reader_relevance import NEGATIVE_CASES, complete_review
from test_reader_findings import SAFETY_MATERIAL


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class ReaderFindingsPostgresTests(unittest.TestCase):
    setUp = answer_fixture.ReaderAnswersPostgresTests.setUp
    tearDown = answer_fixture.ReaderAnswersPostgresTests.tearDown
    insert = answer_fixture.ReaderAnswersPostgresTests.insert
    row = answer_fixture.ReaderAnswersPostgresTests.row
    seed = answer_fixture.ReaderAnswersPostgresTests.seed
    admit = answer_fixture.ReaderAnswersPostgresTests.admit

    def model(self, conn, stage, instructions, data):
        if stage == "digest":
            candidates = data if isinstance(data, list) else data["candidates"]
            return {"items": [{"material_ids": [c["id"]], "fact_ids": [f["id"] for f in c["facts"]],
                               "title": "Находка об уходе за кожей", "summary": "В источнике описано наблюдение для ухода за кожей.",
                               "limitations": "Изучена аннотация.", "significance": "SECRET unverified recommendation"}
                              for c in candidates]}
        if stage == "digest_verify":
            return {"items": [{"index": i, "valid": True, "code": "supported"}
                              for i in range(len(data["items"]))]}
        raise AssertionError("Reader finding should use exactly two editorial stages: " + stage)

    def current(self):
        return digest.current_findings(self.conn)

    def stored(self):
        return self.conn.execute("SELECT material_id,status,content,fact_ids FROM reader_findings ORDER BY id").fetchall()

    def test_two_candidates_are_one_editorial_pair_and_read_only_feed_reuses_exact_findings(self):
        first, ids1 = self.seed(suffix="first")
        second, ids2 = self.seed(suffix="second")
        with patch.object(llm, "call", side_effect=self.model) as model:
            report = digest.prepare_findings(self.conn)
            feed = self.current()
            repeated = self.current()
        self.assertEqual(report["prepared"], 2)
        self.assertEqual(report["errors"], [])
        self.assertEqual([c.args[1] for c in model.call_args_list], ["digest", "digest_verify"])
        self.assertEqual(feed, repeated)
        self.assertEqual({f["id"] for f in feed}, {first["id"], second["id"]})
        self.assertEqual({fid for f in feed for fid in f["fact_ids"]}, set(ids1 + ids2))
        self.assertTrue(all(f["finding_id"] for f in feed))
        self.assertNotIn("significance", repr(feed))
        self.assertNotIn("SECRET", repr(feed))
        for finding in feed:
            self.assertEqual(finding["material_ids"], [finding["id"]])
            self.assertEqual(finding["editorial_version"], digest.EDITORIAL_VERSION)
            self.assertEqual(finding["profile_version"], relevance.load_profile()["version"])
            self.assertEqual(finding["sources"][0]["scope"], "abstract")
            self.assertEqual(finding["sources"][0]["source_role"], "research_candidate")

    def test_unchanged_findings_survive_new_connection_and_never_pay_twice(self):
        import psycopg
        self.seed()
        with patch.object(llm, "call", side_effect=self.model):
            digest.prepare_findings(self.conn)
        expected = self.current()
        with patch.object(llm, "call", side_effect=AssertionError("Current finding is already paid and verified")):
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 0)
            with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                 options=f"-c search_path={self.schema}") as restarted:
                self.assertEqual(digest.current_findings(restarted), expected)
                self.assertEqual(digest.prepare_findings(restarted)["prepared"], 0)
        self.assertEqual(len(self.stored()), 1)

    def test_private_legacy_unverified_and_inactive_facts_cannot_be_reader_findings(self):
        self.seed(public=False, suffix="legacy")
        self.seed(NEGATIVE_CASES[0][1], public=False, owner=41, question="vaginal dryness", suffix="private")
        unverified, _ = self.seed(suffix="unverified")
        self.conn.execute("UPDATE findings SET verified=FALSE WHERE material_id=%s", (unverified["id"],))
        inactive, _ = self.seed(suffix="inactive")
        self.conn.execute("UPDATE facts SET active=FALSE WHERE material_id=%s", (inactive["id"],))
        with patch.object(llm, "call", side_effect=AssertionError("No public verified candidates")):
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 0)
            self.assertEqual(self.current(), [])

    def test_item_rejection_preserves_neighbor_and_rejection_cache_prevents_paid_repeat(self):
        self.seed(suffix="one")
        self.seed(suffix="two")
        def selective(conn, stage, instructions, data):
            if stage == "digest_verify":
                return {"items": [{"index": 0, "valid": True, "code": "supported"},
                                  {"index": 1, "valid": False, "code": "wrong_population"}]}
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=selective) as model:
            digest.prepare_findings(self.conn)
            self.assertEqual(len(self.current()), 1)
            digest.prepare_findings(self.conn)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(sorted(row[1] for row in self.stored()), ["eligible", "excluded"])

    def test_structurally_bad_item_is_filtered_before_verifier_without_losing_good_item(self):
        first, ids = self.seed(suffix="one")
        second, _ = self.seed(suffix="two")
        def mixed(conn, stage, instructions, data):
            if stage == "digest":
                return {"items": [None, {"material_ids": [second["id"]], "fact_ids": [999999],
                                         "title": "Неверная ссылка", "summary": "Нет оснований."},
                                  {"material_ids": [first["id"]], "fact_ids": ids,
                                   "title": "Подтверждённая находка", "summary": "В источнике описано наблюдение."}]}
            self.assertEqual(len(data["items"]), 1)
            return self.model(conn, stage, instructions, data)
        with patch.object(llm, "call", side_effect=mixed):
            digest.prepare_findings(self.conn)
        self.assertEqual([f["id"] for f in self.current()], [first["id"]])

    def test_malformed_verification_cannot_approve_any_item_and_is_cached(self):
        cases = [{"valid": True}, {"items": []},
                 {"items": [{"index": True, "valid": True, "code": "supported"}]},
                 {"items": [{"index": 0, "valid": 1, "code": "supported"}]},
                 {"items": [{"index": 0, "valid": True, "code": []}]},
                 {"items": [{"index": 0, "valid": False, "code": "supported"}]}]
        for index, verdict in enumerate(cases):
            with self.subTest(verdict=verdict):
                candidate, _ = self.seed(suffix="format-" + str(index))
                def malformed(conn, stage, instructions, data):
                    return verdict if stage == "digest_verify" else self.model(conn, stage, instructions, data)
                with patch.object(llm, "call", side_effect=malformed) as model:
                    report = digest.prepare_findings(self.conn)
                    digest.prepare_findings(self.conn)
                self.assertEqual(model.call_count, 2)
                self.assertTrue(report["errors"])
                self.assertFalse(any(f["id"] == candidate["id"] for f in self.current()))

    def test_budget_denial_before_generation_does_not_cache_permanent_editorial_failure(self):
        self.seed()
        with patch.object(llm, "call", side_effect=llm.BudgetExceeded):
            try:
                digest.prepare_findings(self.conn)
            except llm.BudgetExceeded:
                pass
        self.assertEqual(self.stored(), [])
        with patch.object(llm, "call", side_effect=self.model) as model:
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 1)
        self.assertEqual(model.call_count, 2)

    def test_changed_facts_or_source_metadata_hide_old_finding_and_generate_new_snapshot(self):
        candidate, _ = self.seed()
        with patch.object(llm, "call", side_effect=self.model):
            digest.prepare_findings(self.conn)
        old = self.current()[0]
        self.conn.execute("UPDATE facts SET statement=statement||' changed source observation' WHERE material_id=%s", (candidate["id"],))
        self.assertEqual(self.current(), [])
        with patch.object(llm, "call", side_effect=self.model):
            digest.prepare_findings(self.conn)
        second = self.current()[0]
        self.assertNotEqual(second["finding_id"], old["finding_id"])
        self.assertNotEqual(second["input_signature"], old["input_signature"])
        self.conn.execute("UPDATE materials SET published_at='2020-01-01T00:00:00Z' WHERE id=%s", (candidate["id"],))
        self.assertEqual(self.current(), [])
        with patch.object(llm, "call", side_effect=self.model):
            digest.prepare_findings(self.conn)
        self.assertTrue(str(self.current()[0]["sources"][0]["published_at"]).startswith("2020"))
        self.assertEqual(len(self.stored()), 3)

    def test_text_profile_editorial_and_admission_revisions_are_checked_at_read_time(self):
        candidate, _ = self.seed()
        with patch.object(llm, "call", side_effect=self.model):
            digest.prepare_findings(self.conn)
        profile = dict(relevance.load_profile(), version="new-reader-profile")
        with patch.object(relevance, "load_profile", return_value=profile):
            self.assertEqual(self.current(), [])
        with patch.object(digest, "EDITORIAL_VERSION", digest.EDITORIAL_VERSION + 1):
            self.assertEqual(self.current(), [])
        self.conn.execute("UPDATE materials SET text=text||' Updated text.' WHERE id=%s", (candidate["id"],))
        self.assertEqual(self.current(), [])
        candidate["text"] += " Updated text."
        self.admit(candidate)
        with patch.object(llm, "call", side_effect=self.model):
            digest.prepare_findings(self.conn)
        review = {"decision": "excluded", "relevant": False,
                  "evidence_quote": candidate["text"].split(". ")[0] + ".", "reason": "Synthetic explicit exclusion."}
        relevance.store_assessment(self.conn, candidate, relevance.assess(candidate, review=review))
        self.assertEqual(self.current(), [])

    def test_batch_is_capped_ten_and_pagination_does_not_create_findings(self):
        for i in range(12):
            self.seed(suffix="batch-" + str(i))
        with patch.object(llm, "call", side_effect=self.model) as model:
            digest.prepare_findings(self.conn)
            self.assertEqual(len(self.stored()), 10)
            self.assertEqual(model.call_count, 2)
            digest.prepare_findings(self.conn)
        self.assertEqual(len(self.stored()), 12)
        with patch.object(llm, "call", side_effect=AssertionError("Read-only pagination")):
            first = digest.current_findings(self.conn, limit=5, offset=0)
            second = digest.current_findings(self.conn, limit=5, offset=5)
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertTrue({f["finding_id"] for f in first}.isdisjoint(f["finding_id"] for f in second))

    def test_generate_prepares_feed_before_edition_hour_and_digest_reuses_same_canonical_rows(self):
        self.seed()
        now = datetime.now(timezone.utc).replace(hour=5, minute=0)
        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "UTC", "DIGEST_HOUR": "19", "EXA_MONTHLY_USD": "0"}), \
                patch.object(llm, "call", side_effect=self.model) as model:
            self.assertIsNone(digest.generate(self.conn, now))
            feed = self.current()
            self.assertEqual(len(feed), 1)
            did = digest.generate(self.conn, now.replace(hour=20))
            content = self.conn.execute("SELECT content FROM digests WHERE id=%s", (did,)).fetchone()[0]
        self.assertEqual(model.call_count, 2)
        self.assertEqual(content["items"], feed)

    def test_completed_daily_cache_cannot_republish_a_revoked_finding(self):
        candidate, _ = self.seed()
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "UTC", "DIGEST_HOUR": "0", "EXA_MONTHLY_USD": "0"}), \
                patch.object(llm, "call", side_effect=self.model):
            did = digest.generate(self.conn, now)
            self.assertTrue(self.conn.execute("SELECT content FROM digests WHERE id=%s", (did,)).fetchone()[0]["items"])
            self.conn.execute("UPDATE facts SET active=FALSE WHERE material_id=%s", (candidate["id"],))
            digest.generate(self.conn, now + timedelta(minutes=1))
        self.assertEqual(self.conn.execute("SELECT content FROM digests WHERE id=%s", (did,)).fetchone()[0]["items"], [])

    def test_editorial_service_failure_cannot_become_no_updates_after_complete_source_checks(self):
        self.seed()
        sid = self.conn.execute("""INSERT INTO sources(name,kind,url,active)
            VALUES ('Controlled public source','direct_pages','https://example.org/public',TRUE) RETURNING id""").fetchone()[0]
        self.conn.execute("""INSERT INTO source_checks(source_id,purpose,status,stage,planned,complete)
            VALUES (%s,'monitoring','empty','collection',TRUE,TRUE)""", (sid,))
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "UTC", "DIGEST_HOUR": "0", "EXA_MONTHLY_USD": "0"}), \
                patch.object(llm, "call", side_effect=TimeoutError("SECRET provider response")) as model:
            did = digest.generate(self.conn, now)
            repeated = digest.generate(self.conn, now + timedelta(minutes=1))
        self.assertEqual(did, repeated)
        self.assertEqual(model.call_count, 1)
        status, content = self.conn.execute("SELECT status,content FROM digests WHERE id=%s", (did,)).fetchone()
        self.assertNotEqual(status, "ready")
        self.assertEqual(content["items"], [])
        self.assertRegex(content["message"], "[А-Яа-яЁё]")
        self.assertNotRegex(content["message"].lower(), "подходящих обновлений нет|подходящих материалов.*нет")
        self.assertTrue(content.get("editorial_errors"))
        self.assertNotIn("SECRET", repr(content))

    def test_concurrent_worker_cannot_pay_for_the_same_editorial_batch(self):
        import psycopg
        self.seed()
        with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                             options=f"-c search_path={self.schema}") as other:
            def overlapping(conn, stage, instructions, data):
                if stage == "digest":
                    self.assertEqual(digest.prepare_findings(other)["prepared"], 0)
                return self.model(conn, stage, instructions, data)
            with patch.object(llm, "call", side_effect=overlapping) as model:
                digest.prepare_findings(self.conn)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len(self.current()), 1)

    def test_facts_changed_during_verification_cannot_publish_stale_generated_finding(self):
        import psycopg
        candidate, _ = self.seed()
        with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                             options=f"-c search_path={self.schema}") as other:
            def changed(conn, stage, instructions, data):
                if stage == "digest_verify":
                    other.execute("UPDATE facts SET statement=statement||' changed during verification' WHERE material_id=%s",
                                  (candidate["id"],))
                return self.model(conn, stage, instructions, data)
            with patch.object(llm, "call", side_effect=changed):
                digest.prepare_findings(self.conn)
        self.assertEqual(self.current(), [])
        self.assertEqual(self.stored(), [])
        with patch.object(llm, "call", side_effect=self.model):
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 1)

    def test_official_cosmetic_safety_origin_is_preserved_and_forged_host_cannot_publish(self):
        candidate, _ = self.seed(SAFETY_MATERIAL, public=False)
        review = dict(complete_review(candidate, "finished_cosmetics"), human_results=False)
        decision = relevance.assess(candidate, review=review)
        self.assertEqual(decision["status"], "eligible")
        relevance.store_assessment(self.conn, candidate, decision)
        with patch.object(llm, "call", side_effect=self.model):
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 1)
        finding = self.current()[0]
        self.assertEqual(finding["sources"][0]["kind"], "regulatory")
        self.assertEqual(finding["sources"][0]["source_role"], "official_safety")
        self.conn.execute("UPDATE materials SET url='https://fda.gov.example.org/synthetic-forgery' WHERE id=%s", (candidate["id"],))
        self.assertEqual(self.current(), [])
        with patch.object(llm, "call", side_effect=AssertionError("Forged official origin is not public")):
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 0)
        forged = dict(SAFETY_MATERIAL, url="https://fda.gov.example.org/synthetic-ingest-forgery")
        mid = collector.ingest(self.conn, forged)
        self.assertNotEqual(self.conn.execute("SELECT source_role FROM materials WHERE id=%s", (mid,)).fetchone()[0],
                            "official_safety")

    def test_complete_edition_refreshes_new_findings_and_later_failed_source_without_rewriting_findings(self):
        first, _ = self.seed(suffix="earlier")
        sid = self.conn.execute("""INSERT INTO sources(name,kind,url,active)
            VALUES ('Controlled source','direct_pages','https://example.org/public',TRUE) RETURNING id""").fetchone()[0]
        self.conn.execute("""INSERT INTO source_checks(source_id,purpose,status,stage,planned,complete)
            VALUES (%s,'monitoring','empty','collection',TRUE,TRUE)""", (sid,))
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "UTC", "DIGEST_HOUR": "0", "EXA_MONTHLY_USD": "0"}), \
                patch.object(llm, "call", side_effect=self.model) as model:
            did = digest.generate(self.conn, now)
            status, initial = self.conn.execute("SELECT status,content FROM digests WHERE id=%s", (did,)).fetchone()
            self.assertEqual(status, "ready")
            second, _ = self.seed(suffix="later")
            self.assertEqual(digest.generate(self.conn, now + timedelta(minutes=1)), did)
            refreshed = self.conn.execute("SELECT content FROM digests WHERE id=%s", (did,)).fetchone()[0]
            self.assertEqual({f["id"] for f in refreshed["items"]}, {first["id"], second["id"]})
            self.assertIn(initial["items"][0], refreshed["items"])
            self.conn.execute("""INSERT INTO source_checks(source_id,purpose,status,stage,planned,complete,code,finished_at)
                VALUES (%s,'monitoring','failed','collection',TRUE,FALSE,'timeout',%s)""", (sid, now + timedelta(seconds=30)))
            digest.generate(self.conn, now + timedelta(minutes=2))
        self.assertEqual(model.call_count, 4)
        status, final = self.conn.execute("SELECT status,content FROM digests WHERE id=%s", (did,)).fetchone()
        self.assertEqual(status, "partial")
        self.assertEqual(final["items"], refreshed["items"])
        self.assertFalse(final["coverage"]["complete"])

    def test_ten_blocked_catalogue_candidates_cannot_starve_the_eleventh_useful_material(self):
        useful, _ = self.seed(suffix="useful-first")
        for i in range(10):
            blocked, _ = self.seed(suffix="blocked-" + str(i))
            self.conn.execute("UPDATE materials SET url=%s WHERE id=%s",
                              ("https://world.openbeautyfacts.org/product/synthetic-" + str(i), blocked["id"]))
        with patch.object(llm, "call", side_effect=self.model) as model:
            digest.prepare_findings(self.conn)
            digest.prepare_findings(self.conn)
            digest.prepare_findings(self.conn)
        self.assertEqual(model.call_count, 2)
        self.assertEqual([f["id"] for f in self.current()], [useful["id"]])

    def test_invalid_configuration_never_caches_a_paid_failure_and_valid_config_recovers(self):
        import llm as model_service
        self.seed()
        for bad in ("oops", "NaN", "Infinity", "0"):
            with self.subTest(value=bad), patch.dict(os.environ, {"MODEL_MONTHLY_USD": bad}), \
                    patch.object(model_service, "call", side_effect=AssertionError("Preflight before any model call")):
                report = digest.prepare_findings(self.conn)
                self.assertTrue(report["errors"])
                self.assertEqual(self.stored(), [])
        with patch.object(llm, "call", side_effect=self.model) as model:
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 1)
        self.assertEqual(model.call_count, 2)

    def test_all_semantically_not_useful_items_allow_honest_complete_empty_edition(self):
        self.seed()
        sid = self.conn.execute("""INSERT INTO sources(name,kind,url,active)
            VALUES ('Controlled source','direct_pages','https://example.org/public',TRUE) RETURNING id""").fetchone()[0]
        self.conn.execute("""INSERT INTO source_checks(source_id,purpose,status,stage,planned,complete)
            VALUES (%s,'monitoring','empty','collection',TRUE,TRUE)""", (sid,))
        def not_useful(conn, stage, instructions, data):
            if stage == "digest_verify":
                return {"items": [{"index": i, "valid": False, "code": "not_useful"}
                                  for i in range(len(data["items"]))]}
            return self.model(conn, stage, instructions, data)
        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "UTC", "DIGEST_HOUR": "0", "EXA_MONTHLY_USD": "0"}), \
                patch.object(llm, "call", side_effect=not_useful) as model:
            did = digest.generate(self.conn, datetime.now(timezone.utc) + timedelta(seconds=1))
        self.assertEqual(model.call_count, 2)
        status, content = self.conn.execute("SELECT status,content FROM digests WHERE id=%s", (did,)).fetchone()
        self.assertEqual(status, "ready")
        self.assertEqual(content["items"], [])
        self.assertEqual(content["editorial_errors"], [])
        self.assertTrue(content["coverage"]["complete"])
        self.assertRegex(content["message"].lower(), "подходящих обновлений нет|подходящих материалов.*нет")

    def test_read_gate_rejects_forged_cached_foreign_fact_and_material_references(self):
        own, own_ids = self.seed(suffix="own")
        other, other_ids = self.seed(suffix="foreign")
        with patch.object(llm, "call", side_effect=self.model):
            digest.prepare_findings(self.conn)
        rid, original = self.conn.execute("SELECT id,content FROM reader_findings WHERE material_id=%s", (own["id"],)).fetchone()
        cases = [(dict(original, fact_ids=other_ids), other_ids),
                 (dict(original, material_ids=[other["id"]]), own_ids),
                 (dict(original, fact_ids=[]), [])]
        with patch.object(llm, "call", side_effect=AssertionError("Cached read is read-only")):
            for forged, column_ids in cases:
                with self.subTest(content=forged):
                    self.conn.execute("UPDATE reader_findings SET content=%s,fact_ids=%s WHERE id=%s",
                                      (db.jsonb(forged), column_ids, rid))
                    self.assertEqual(digest.current_findings(self.conn, [own["id"]]), [])
                    self.assertEqual(len(digest.current_findings(self.conn, [other["id"]])), 1)
            self.conn.execute("UPDATE reader_findings SET content=%s,fact_ids=%s WHERE id=%s",
                              (db.jsonb(original), own_ids, rid))
        self.assertEqual(len(digest.current_findings(self.conn, [own["id"]])), 1)

    def test_seven_irrelevant_control_materials_and_two_useful_findings_keep_original_data(self):
        negative_ids=[]
        for name, original in NEGATIVE_CASES:
            candidate, _ = self.seed(original, public=False, suffix="matrix-" + name)
            negative_ids.append(candidate["id"])
            forged = relevance.assess(candidate, review=complete_review(candidate))
            self.assertEqual(forged["status"], "excluded", name)
            relevance.store_assessment(self.conn, candidate, forged)
        first, _ = self.seed(suffix="matrix-useful-one")
        second, _ = self.seed(suffix="matrix-useful-two")
        with patch.object(llm, "call", side_effect=self.model) as model:
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 2)
        self.assertEqual(model.call_count, 2)
        self.assertEqual({f["id"] for f in self.current()}, {first["id"], second["id"]})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials WHERE id=ANY(%s)", (negative_ids,)).fetchone()[0], 7)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE active").fetchone()[0], 9)
