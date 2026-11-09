"""One QA-003 acceptance cycle on an isolated PostgreSQL schema, entirely offline."""

import os
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

import db
import digest
import llm
import research
from bot import main as bot
from collector import adapters
from collector import exa
from collector import main as collector
from web.main import Handler
from worker.main import process_material
from test_direct_sources import PAGES, html_fixture, source_fixture
from test_reader_relevance import complete_review


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class SourceAcceptancePostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.schema = "qa003_" + uuid4().hex
        self.admin = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                    options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=3000")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.close()

    def test_three_direct_sources_through_verified_answer_history_digest_and_delivery(self):
        collector.seed_sources(self.conn)
        for index in range(3):
            fixture = source_fixture(index)
            fixture["config"]["pages"][0]["topics"] = ["retinol", "acne"]
            self.conn.execute("UPDATE sources SET url=%s,active=TRUE,config=%s WHERE name=%s",
                              (fixture["url"], db.jsonb(fixture["config"]), fixture["name"]))
        pages = {url: html_fixture(index) for index, (name, url, role) in enumerate(PAGES)}
        non_target = {kind: (lambda source, query=None: iter(())) for kind in collector.READERS
                      if kind not in {"direct_pages", "exa"}}
        quote = "Twelve fixture observations were recorded."

        def model(conn, stage, instructions, data):
            if stage == "reader_review":
                return complete_review({"text": data["text"]})
            if stage == "triage":
                return {"relevant": True, "important": True, "kind": "research", "summary": "Контрольный материал об уходе",
                        "topics": ["retinol acne"]}
            if stage == "extract":
                return {"facts": [{"statement": "Retinol acne: twelve fixture observations", "quote": quote, "payload": {}}],
                        "limitations": ["Синтетический материал для проверки"]}
            if stage == "verify":
                return {"accepted_indices": [0], "limitations": []}
            if stage == "search_plan":
                return {"query": "retinol acne", "terms": ["retinol", "acne"], "clarification": "", "subject_identified": True}
            if stage in {"synthesis", "answer"}:
                return {"claims": [{"text": "Подтверждённый факт " + str(fact["id"]), "fact_ids": [fact["id"]]} for fact in data["facts"]],
                        "limitations": ["Справочные страницы и публичное резюме, а не три клинических исследования"],
                        "missing_data": [], "contradictions": []}
            if stage in {"synthesis_verify", "answer_verify", "digest_verify"}:
                return {"valid": True}
            if stage == "digest":
                return {"items": [{"material_ids": [candidate["id"]], "title": "Материал об уходе",
                                    "summary": "Контрольный пересказ материала", "limitations": "Изучен доступный фрагмент"}
                                   for candidate in data]}
            raise AssertionError("Unexpected model stage: " + stage)

        settings = {"EXA_API_KEY": "", "EXA_MONTHLY_USD": "0", "EXA_SCOPE_USD": "0",
                    "DIGEST_TIMEZONE": "UTC", "DIGEST_HOUR": "0", "TELEGRAM_CHAT_IDS": "42",
                    "TELEGRAM_TOKEN": "testOnlyDeliveryToken"}
        with patch.dict(os.environ, settings), patch.dict(collector.READERS, non_target), \
                patch.object(adapters, "fetch", side_effect=lambda url, **kwargs: pages[url]), \
                patch.object(exa, "post_json", side_effect=AssertionError("Exa is outside this controlled cycle")) as exa_transport, \
                patch.object(llm, "call", side_effect=model):
            collection = collector.collect(self.conn, force=True)
            self.assertEqual(len(collection["material_ids"]), 3)
            for mid in collection["material_ids"]:
                row = self.conn.execute("SELECT id,title,text,text_scope FROM materials WHERE id=%s", (mid,)).fetchone()
                process_material(self.conn, row, force=True)

            answer = research.answer(self.conn, "retinol acne", owner_telegram_id=42)
            self.assertEqual(answer["status"], "completed")
            self.assertEqual(len(answer["claims"]), 3)
            self.assertEqual({source["url"] for source in answer["sources"]}, set(pages))
            self.assertEqual({source["source_role"] for source in answer["sources"]}, {role for name, url, role in PAGES})

            handler = Handler.__new__(Handler)
            handler.path = "/api/miniapp/answers"
            handler.command = "GET"
            handler.miniapp_user = Mock(return_value=42)
            handler.respond = Mock()
            with patch.object(db, "connect", return_value=nullcontext(self.conn)):
                handler.do_GET()
            self.assertEqual(handler.respond.call_args.args[0], 200)
            history = handler.respond.call_args.args[1]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["content"]["status"], "completed")
            self.assertEqual(len(history[0]["content"]["sources"]), 3)

            did = digest.generate(self.conn, datetime.now(timezone.utc) + timedelta(seconds=1))
            status, content = self.conn.execute("SELECT status,content FROM digests WHERE id=%s", (did,)).fetchone()
            self.assertEqual(status, "ready")
            self.assertTrue(content["coverage"]["complete"])
            self.assertEqual(len(content["items"]), 3)
            digest_sources = [source for item in content["items"] for source in item["sources"]]
            self.assertEqual({source["url"] for source in digest_sources}, set(pages))
            self.assertTrue(all(str(source["published_at"]).startswith("2004-02-03") for source in digest_sources))
            with patch.object(bot, "send") as send:
                self.assertEqual(bot.deliver(self.conn), 1)
            self.assertTrue(send.called)
            self.assertTrue(all(call.args[0] == 42 for call in send.call_args_list))
            exa_transport.assert_not_called()

        # Independent reads of the produced database, not the writer's return values.
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE active").fetchone()[0], 3)
        rows = self.conn.execute("""SELECT m.url,m.source_role,m.authors,m.published_at,m.text,f.quote
                                    FROM materials m JOIN facts f ON f.material_id=m.id WHERE f.active""").fetchall()
        expected_roles = {url: role for name, url, role in PAGES}
        for url, role, author, published, text, actual_quote in rows:
            self.assertEqual(role, expected_roles[url])
            self.assertIn("Fixture", author)
            self.assertEqual(str(published.date()), "2004-02-03")
            self.assertIn(actual_quote, text)
        self.assertEqual(self.conn.execute("SELECT owner_telegram_id FROM answers").fetchone(), (42,))
        self.assertEqual(self.conn.execute("SELECT status FROM digest_deliveries WHERE digest_id=%s AND chat_id=42", (did,)).fetchone(), ("sent",))


if __name__ == "__main__":
    unittest.main()
