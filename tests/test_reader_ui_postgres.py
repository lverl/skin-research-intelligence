"""UI-001: reader APIs use real current findings and owner-scoped SQL."""
import io
import json
import os
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import db
import digest
import llm
from web.main import Handler
import test_reader_findings_postgres as finding_fixture


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class ReaderUiPostgresTests(unittest.TestCase):
    tearDown = finding_fixture.ReaderFindingsPostgresTests.tearDown
    insert = finding_fixture.ReaderFindingsPostgresTests.insert
    row = finding_fixture.ReaderFindingsPostgresTests.row
    seed = finding_fixture.ReaderFindingsPostgresTests.seed
    admit = finding_fixture.ReaderFindingsPostgresTests.admit
    model = finding_fixture.ReaderFindingsPostgresTests.model

    def setUp(self):
        finding_fixture.ReaderFindingsPostgresTests.setUp(self)
        self.conn.execute("INSERT INTO participants(telegram_id,role,miniapp_enabled) VALUES (41,'owner',TRUE),(42,'owner',TRUE)")

    def get(self, path, owner=41):
        handler = Handler.__new__(Handler)
        handler.path = path
        handler.command = "GET"
        handler.miniapp_user = Mock(return_value=owner)
        handler.respond = Mock()
        with patch.object(db, "connect", return_value=nullcontext(self.conn)), \
                patch.object(llm, "call", side_effect=AssertionError("GET cannot generate or review")), \
                patch.object(digest, "prepare_findings", side_effect=AssertionError("GET only reads prepared findings")):
            handler.do_GET()
        self.assertEqual(handler.respond.call_count, 1)
        return handler.respond.call_args.args[:2]

    def post(self, path, payload, owner=41):
        body = json.dumps(payload).encode()
        handler = Handler.__new__(Handler)
        handler.path = path
        handler.command = "POST"
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json", "Host": "reader.test"}
        handler.rfile = io.BytesIO(body)
        handler.miniapp_user = Mock(return_value=owner)
        handler.respond = Mock()
        with patch.object(db, "connect", return_value=nullcontext(self.conn)), \
                patch.object(llm, "call", side_effect=AssertionError("A bookmark cannot cause paid research")):
            handler.do_POST()
        self.assertEqual(handler.respond.call_count, 1)
        return handler.respond.call_args.args[:2]

    def prepare(self, count=1):
        candidates = [self.seed(suffix="ui-" + str(i))[0] for i in range(count)]
        with patch.object(llm, "call", side_effect=self.model):
            for _ in range((count + 9) // 10):
                digest.prepare_findings(self.conn)
        return candidates

    def bookmark(self, candidate, owner=41):
        self.conn.execute("INSERT INTO saved_materials(owner_telegram_id,material_id) VALUES (%s,%s)", (owner, candidate["id"]))

    def test_feed_defaults_to_ten_current_canonical_findings_and_explicit_offset_reads_next_page(self):
        candidates = self.prepare(12)
        expected = digest.current_findings(self.conn, limit=10)
        status, first = self.get("/api/miniapp/feed")
        self.assertEqual(status, 200)
        self.assertEqual(first, expected)
        self.assertEqual(len(first), 10)
        status, second = self.get("/api/miniapp/feed?limit=10&offset=10")
        self.assertEqual(status, 200)
        self.assertEqual(len(second), 2)
        self.assertEqual({f["id"] for f in first + second}, {c["id"] for c in candidates})
        self.assertTrue({f["id"] for f in first}.isdisjoint(f["id"] for f in second))
        self.assertEqual(len(self.get("/api/miniapp/feed?limit=999&offset=0")[1]), 10)
        self.assertEqual(len(self.get("/api/miniapp/feed?limit=2&offset=1")[1]), 2)

    def test_feed_excludes_legacy_private_and_stale_findings_without_read_time_repair(self):
        current = self.prepare()[0]
        self.seed(public=False, suffix="unreviewed")
        self.conn.execute("UPDATE materials SET text=text||' Changed source text.' WHERE id=%s", (current["id"],))
        self.assertEqual(self.get("/api/miniapp/feed"), (200, []))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM reader_findings").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials").fetchone()[0], 2)

    def test_question_history_defaults_and_caps_at_five_and_is_scoped_to_verified_owner(self):
        base = datetime.now(timezone.utc)
        for owner, count in ((41, 7), (42, 3)):
            for i in range(count):
                self.conn.execute("INSERT INTO answers(question,content,owner_telegram_id,created_at) VALUES (%s,%s,%s,%s)",
                    ("Question " + str(owner) + ":" + str(i), db.jsonb({"status": "failed", "claims": []}), owner,
                     base + timedelta(seconds=i)))
        status, page = self.get("/api/miniapp/answers")
        self.assertEqual(status, 200)
        self.assertEqual([r["question"] for r in page], ["Question 41:" + str(i) for i in range(6, 1, -1)])
        remainder = self.get("/api/miniapp/answers?limit=5&offset=5")[1]
        self.assertEqual([r["question"] for r in remainder], ["Question 41:1", "Question 41:0"])
        self.assertEqual(len(self.get("/api/miniapp/answers?limit=999")[1]), 5)
        other = self.get("/api/miniapp/answers", owner=42)[1]
        self.assertEqual(len(other), 3)
        self.assertTrue(all(r["question"].startswith("Question 42:") for r in other))

    def test_saved_current_entry_reuses_canonical_finding_and_other_owners_bookmarks_stay_private(self):
        first, second = self.prepare(2)
        self.bookmark(first)
        self.bookmark(second, owner=42)
        status, rows = self.get("/api/miniapp/saved")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in rows], [first["id"]])
        canonical = digest.current_findings(self.conn, [first["id"]])[0]
        for key, value in canonical.items():
            self.assertEqual(rows[0][key], value, key)
        self.assertTrue(rows[0]["saved_at"])
        self.assertEqual([r["id"] for r in self.get("/api/miniapp/saved", owner=42)[1]], [second["id"]])

    def test_saved_legacy_entry_is_preserved_with_review_notice_and_no_unchecked_summary(self):
        legacy, _ = self.seed(public=False, suffix="legacy-bookmark")
        self.conn.execute("UPDATE findings SET summary='SECRET unchecked legacy clinical summary' WHERE material_id=%s", (legacy["id"],))
        self.bookmark(legacy)
        status, rows = self.get("/api/miniapp/saved")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], legacy["id"])
        self.assertEqual(rows[0]["url"], legacy["url"])
        self.assertEqual(rows[0]["review_status"], "needs_review")
        self.assertEqual(rows[0].get("summary", ""), "")
        self.assertTrue(rows[0]["notice"])
        self.assertNotIn("SECRET", repr(rows))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM saved_materials").fetchone()[0], 1)

    def test_new_save_requires_current_finding_and_rejects_guessed_or_other_owners_old_material(self):
        qualified = self.prepare()[0]
        legacy, _ = self.seed(public=False, suffix="not-public")
        self.bookmark(legacy, owner=42)
        for mid in (999999, legacy["id"]):
            with self.subTest(material_id=mid):
                status, data = self.post("/api/miniapp/save", {"material_id": mid})
                self.assertEqual(status, 404)
                self.assertTrue(data["error"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM saved_materials WHERE owner_telegram_id=41").fetchone()[0], 0)
        status, data = self.post("/api/miniapp/save", {"material_id": qualified["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["saved"])
        self.assertEqual(self.post("/api/miniapp/save", {"material_id": qualified["id"]})[0], 200)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM saved_materials WHERE owner_telegram_id=41").fetchone()[0], 1)

    def test_owner_can_resave_existing_legacy_bookmark_without_new_admission_or_duplicate(self):
        legacy, _ = self.seed(public=False, suffix="own-legacy")
        self.bookmark(legacy)
        status, data = self.post("/api/miniapp/save", {"material_id": legacy["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["saved"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM saved_materials").fetchone()[0], 1)
        self.assertEqual(self.get("/api/miniapp/saved")[1][0]["review_status"], "needs_review")

    def test_revoked_current_finding_disappears_from_feed_but_own_saved_entry_gets_review_notice(self):
        current = self.prepare()[0]
        self.bookmark(current)
        self.conn.execute("UPDATE facts SET active=FALSE WHERE material_id=%s", (current["id"],))
        self.assertEqual(self.get("/api/miniapp/feed"), (200, []))
        saved = self.get("/api/miniapp/saved")[1]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["review_status"], "needs_review")
        self.assertEqual(saved[0].get("summary", ""), "")
        self.assertEqual(self.post("/api/miniapp/save", {"material_id": current["id"]})[0], 200)
        self.assertEqual(self.post("/api/miniapp/save", {"material_id": current["id"]}, owner=42)[0], 404)
