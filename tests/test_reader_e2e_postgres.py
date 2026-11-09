"""QA-004: one offline reader workflow across real admission, facts and outputs."""
import copy
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import db
import digest
import llm
import relevance
import research
from collector import main as collector
from worker import main as worker
from bot import main as bot
import test_reader_ui_postgres as ui_fixture
from test_reader_relevance import NEGATIVE_CASES, POSITIVE_CASES, complete_review


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class ReaderEndToEndPostgresTests(unittest.TestCase):
    setUp = ui_fixture.ReaderUiPostgresTests.setUp
    tearDown = ui_fixture.ReaderUiPostgresTests.tearDown
    insert = ui_fixture.ReaderUiPostgresTests.insert
    row = ui_fixture.ReaderUiPostgresTests.row
    seed = ui_fixture.ReaderUiPostgresTests.seed
    admit = ui_fixture.ReaderUiPostgresTests.admit
    get = ui_fixture.ReaderUiPostgresTests.get
    post = ui_fixture.ReaderUiPostgresTests.post

    def model(self, conn, stage, instructions, data):
        if stage == "reader_review":
            return complete_review({"text": data["text"]})
        if stage == "triage":
            return {"relevant": True, "important": True, "kind": "research", "topics": ["pigmentation"],
                    "summary": "Взрослые участницы в синтетическом исследовании ухода при пигментации."}
        if stage == "extract":
            quote = data["text"].split(". ")[0] + "."
            return {"facts": [{"statement": quote, "quote": quote, "payload": {"synthetic_fixture": True}}], "limitations": []}
        if stage == "verify":
            return {"accepted_indices": [0], "limitations": []}
        if stage == "search_plan":
            return {"subject_identified": True, "query": "facial pigmentation", "terms": ["pigmentation"], "clarification": ""}
        if stage in {"answer", "synthesis"}:
            return {"claims": [{"text": "Источник описывает применение ухода при пигментации кожи лица у взрослых.",
                                "fact_ids": [fact["id"]]} for fact in data["facts"]],
                    "limitations": [], "contradictions": [], "missing_data": []}
        if stage in {"answer_verify", "synthesis_verify"}:
            return {"claims": [{"index": i, "valid": True, "code": "supported"}
                               for i in range(len(data["answer"]["claims"]))], "notes_valid": True}
        if stage == "digest":
            return {"items": [{"material_ids": [candidate["id"]], "fact_ids": [f["id"] for f in candidate["facts"]],
                               "title": ("Ретинол" if "retinol" in candidate["title"].lower() else "Крем") +
                                        " при пигментации исследован у взрослых участниц",
                               "summary": "Источник описывает изученный уход и популяцию участниц.",
                               "limitations": "Изучена аннотация."} for candidate in data]}
        if stage == "digest_verify":
            return {"items": [{"index": i, "valid": True, "code": "supported"} for i in range(len(data["items"]))]}
        raise AssertionError("Unexpected external stage in end-to-end fixture: " + stage)

    def ingest_positive(self, suffix=""):
        candidate = copy.deepcopy(POSITIVE_CASES[0][2])
        candidate["url"] += "-e2e-" + suffix
        candidate["title"] += " " + suffix
        if suffix == "two":
            candidate["title"] = "Topical retinol for adult facial pigmentation"
            candidate["text"] = ("Synthetic adult women received topical retinol for facial pigmentation. "
                                 "The controlled human clinical study reports results of topical facial care.")
        candidate["id"] = collector.ingest(self.conn, candidate)
        return candidate

    def question(self):
        collection = {"material_ids": [], "failed_sources": [], "checks": [{"status": "empty", "planned": True, "complete": True}]}
        with patch.object(collector, "search", return_value=collection):
            return research.answer(self.conn, "что известно о пигментации кожи лица?", owner_telegram_id=41)

    def test_seven_negative_controls_and_two_useful_materials_flow_to_one_shared_reader_cache(self):
        import psycopg
        raw = []
        for _, original in NEGATIVE_CASES:
            candidate = copy.deepcopy(original)
            candidate["id"] = collector.ingest(self.conn, candidate)
            raw.append(candidate)
        positives = [self.ingest_positive("one"), self.ingest_positive("two")]
        source = self.conn.execute("""INSERT INTO sources(name,kind,url,active)
            VALUES ('Controlled reader source','direct_pages','https://example.org/public',TRUE) RETURNING id""").fetchone()[0]
        self.conn.execute("""INSERT INTO source_checks(source_id,purpose,status,stage,planned,complete)
            VALUES (%s,'monitoring','empty','collection',TRUE,TRUE)""", (source,))
        with patch.object(llm, "call", side_effect=self.model) as model:
            worker.process_new(self.conn)
            self.assertEqual([c.args[1] for c in model.call_args_list].count("reader_review"), 2)
            self.assertFalse(any(c.args[1].startswith("synthesis") for c in model.call_args_list))
            answer = self.question()
            self.assertEqual(answer["status"], "completed", answer)
            self.assertEqual(len(answer["claims"]), 2)
            self.assertFalse(any(c.args[1].startswith("synthesis") for c in model.call_args_list))
            reports = research.refresh_topics(self.conn)
            self.assertTrue(any(report["status"] == "ready" for report in reports), reports)
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 2)
        feed_status, feed = self.get("/api/miniapp/feed")
        self.assertEqual(feed_status, 200)
        self.assertEqual({item["id"] for item in feed}, {c["id"] for c in positives})
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "UTC", "DIGEST_HOUR": "0", "EXA_MONTHLY_USD": "0"}), \
                patch.object(llm, "call", side_effect=AssertionError("Shared editorial and topic snapshots are cached")):
            did = digest.generate(self.conn, now)
            edition = self.conn.execute("SELECT status,content FROM digests WHERE id=%s", (did,)).fetchone()
            self.assertEqual(edition[0], "ready")
            self.assertEqual(edition[1]["items"], feed)
            self.assertEqual(self.post("/api/miniapp/save", {"material_id": positives[0]["id"]})[0], 200)
            saved = self.get("/api/miniapp/saved")[1]
            self.assertEqual(saved[0]["finding_id"], next(f["finding_id"] for f in feed if f["id"] == positives[0]["id"]))
            with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                 options=f"-c search_path={self.schema}") as restarted:
                self.assertEqual(digest.prepare_findings(restarted)["prepared"], 0)
                self.assertEqual(digest.current_findings(restarted), feed)
                research.refresh_topics(restarted)
                self.assertEqual(digest.generate(restarted, now + timedelta(minutes=1)), did)
        with patch.dict(os.environ, {"TELEGRAM_CHAT_IDS": "41", "TELEGRAM_TOKEN": "offline-test-token"}), \
                patch.object(bot, "send") as delivery:
            self.assertEqual(bot.deliver(self.conn), 1)
            self.assertTrue(delivery.called)
            self.assertTrue(all(call.args[0] == 41 for call in delivery.call_args_list))
            rendered = "\n".join(call.args[1] for call in delivery.call_args_list)
            for finding in feed:
                self.assertEqual(rendered.count(finding["title"]), 1)
                self.assertEqual(rendered.count(finding["url"]), 1)
            self.assertTrue(all(candidate["url"] not in rendered for candidate in raw))
            sent_calls = delivery.call_count
            self.assertEqual(bot.deliver(self.conn), 0)
            self.assertEqual(delivery.call_count, sent_calls)
        self.assertEqual(self.conn.execute("SELECT status FROM digest_deliveries WHERE digest_id=%s AND chat_id=41", (did,)).fetchone(),
                         ("sent",))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials").fetchone()[0], 9)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE active").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE material_id=ANY(%s)", ([c["id"] for c in raw],)).fetchone()[0], 0)
        for candidate in raw:
            self.assertEqual(self.conn.execute("SELECT text FROM materials WHERE id=%s", (candidate["id"],)).fetchone()[0], candidate["text"])
            self.assertEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "excluded")

    def test_background_failure_keeps_committed_facts_and_ready_answer_then_recovers_separately(self):
        candidate = self.ingest_positive("deferred-error")
        with patch.object(llm, "call", side_effect=self.model):
            worker.process_new(self.conn)
            ready = self.question()
        self.assertEqual(ready["status"], "completed")
        with patch.object(llm, "call", side_effect=TimeoutError("SECRET background provider failure")):
            reports = research.refresh_topics(self.conn)
        self.assertTrue(any(report["status"] == "failed" for report in reports))
        self.assertEqual(self.conn.execute("SELECT content->>'status' FROM answers ORDER BY id DESC LIMIT 1").fetchone()[0], "completed")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE material_id=%s AND active", (candidate["id"],)).fetchone()[0], 1)
        self.assertNotIn("SECRET", repr(reports))
        retry = self.conn.execute("SELECT next_retry_at FROM topic_refresh WHERE topic='pigmentation'").fetchone()[0]
        with patch.object(llm, "call", side_effect=self.model):
            reports = research.refresh_topics(self.conn, now=retry + timedelta(seconds=1))
        self.assertTrue(any(report["status"] == "ready" for report in reports), reports)
        self.assertEqual(self.conn.execute("SELECT content->>'status' FROM answers ORDER BY id DESC LIMIT 1").fetchone()[0], "completed")

    def test_legacy_verified_facts_are_held_until_targeted_private_question_without_automatic_publication(self):
        candidate, ids = self.seed(public=False, count=4, suffix="legacy-held")
        relevance.record_candidate(self.conn, candidate["id"])
        with patch.object(llm, "call", side_effect=AssertionError("Legacy verification alone cannot make a public finding")):
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 0)
        self.assertEqual(self.get("/api/miniapp/feed"), (200, []))
        with patch.object(llm, "call", side_effect=self.model) as model:
            answer = research.answer(self.conn, "пигментация", owner_telegram_id=41)
        self.assertEqual(answer["status"], "completed")
        self.assertEqual({fid for claim in answer["claims"] for fid in claim["fact_ids"]}, set(ids))
        self.assertEqual([call.args[1] for call in model.call_args_list], ["reader_review", "answer", "answer_verify"])
        self.assertEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "needs_review")
        self.assertEqual(self.get("/api/miniapp/feed"), (200, []))
        with patch.object(llm, "call", side_effect=AssertionError("Private admission cannot publish or synthesize globally")):
            self.assertEqual(digest.prepare_findings(self.conn)["prepared"], 0)
            reports = research.refresh_topics(self.conn)
        self.assertTrue(all(report["status"] == "unchanged" for report in reports))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM conclusions WHERE is_current").fetchone()[0], 0)
        self.assertEqual(digest.current_findings(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE material_id=%s", (candidate["id"],)).fetchone()[0], 4)
