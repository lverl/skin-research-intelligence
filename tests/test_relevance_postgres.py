"""FIX-001 persistence acceptance in a disposable schema of a dedicated test DB.

No production connection, external HTTP, model call, or budget mutation is used.
The PostgreSQL runner (role C) supplies TEST_DATABASE_URL explicitly.
"""

import copy
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import db
import llm
import relevance
from collector import main as collector
from test_reader_relevance import NEGATIVE_CASES, POSITIVE_CASES, complete_review


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class ReaderRelevancePostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        test_url = os.environ["TEST_DATABASE_URL"]
        database = urlsplit(test_url).path.rstrip("/").split("/")[-1]
        if not database.endswith("_test"):
            raise RuntimeError("Reader relevance tests require a dedicated *_test database")
        self.schema = "reader_relevance_" + uuid4().hex
        self.admin = psycopg.connect(test_url, autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn = psycopg.connect(
            test_url, autocommit=True,
            options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=3000",
        )
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.close()

    def candidate(self):
        candidate = copy.deepcopy(POSITIVE_CASES[0][2])
        source_id = self.conn.execute(
            "INSERT INTO sources(name,kind,url,active) VALUES (%s,'manual',%s,FALSE) RETURNING id",
            ("Synthetic reader fixture", "https://example.org/reader-fixture-source"),
        ).fetchone()[0]
        candidate["id"] = self.conn.execute(
            """INSERT INTO materials(source_id,url,title,text,text_scope,source_role,status)
               VALUES (%s,%s,%s,%s,%s,%s,'done') RETURNING id""",
            (source_id, candidate["url"], candidate["title"], candidate["text"],
             candidate["text_scope"], candidate["source_role"]),
        ).fetchone()[0]
        return candidate

    def test_an_unassessed_legacy_material_has_no_implicit_admission(self):
        candidate = self.candidate()
        self.conn.execute(
            """INSERT INTO findings(material_id,summary,model,prompt_version,kind,verified)
               VALUES (%s,'Legacy summary','fixture','legacy','research',TRUE)""", (candidate["id"],),
        )
        self.conn.execute(
            "INSERT INTO facts(material_id,statement,quote) VALUES (%s,%s,%s)",
            (candidate["id"], "Synthetic legacy statement", candidate["text"]),
        )
        self.assertIsNone(relevance.get_current_assessment(self.conn, candidate))

    def test_collector_ingest_preserves_all_seven_raw_sources_and_early_exclusions(self):
        with patch.object(llm, "call", side_effect=AssertionError("Early collection admission cannot make paid calls")) as paid:
            for name, original in NEGATIVE_CASES:
                with self.subTest(case=name):
                    candidate = copy.deepcopy(original)
                    mid = collector.ingest(self.conn, candidate)
                    stored = self.conn.execute(
                        "SELECT id,title,text,text_scope,source_role FROM materials WHERE id=%s", (mid,),
                    ).fetchone()
                    self.assertEqual(stored[1:4], (candidate["title"], candidate["text"], candidate["text_scope"]))
                    current = dict(candidate, id=mid)
                    decision = relevance.get_current_assessment(self.conn, current)
                    self.assertEqual(decision["status"], "excluded")
                    self.assertTrue(decision["code"])
                    self.assertEqual(collector.ingest(self.conn, candidate), mid)
                    repeated = relevance.get_current_assessment(self.conn, current)
                    self.assertEqual(repeated["status"], "excluded")
                    self.assertEqual(repeated["code"], decision["code"])
            paid.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials").fetchone()[0], 7)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM material_assessments").fetchone()[0], 7)

    def test_outside_profile_question_admission_cannot_open_public_scope_after_restart(self):
        import psycopg
        candidate = copy.deepcopy(NEGATIVE_CASES[0][1])
        candidate["id"] = collector.ingest(self.conn, candidate)
        self.assertEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "excluded")
        question = "Что известно о лазере при сухости влагалища?"
        review = dict(complete_review(candidate, "aesthetic_procedures"),
                      subject="vaginal dryness", application="vaginal")
        private = relevance.assess(candidate, purpose="question", owner_telegram_id=41,
                                   question=question, review=review)
        self.assertEqual(private["status"], "eligible")
        relevance.store_assessment(self.conn, candidate, private)
        with psycopg.connect(
            os.environ["TEST_DATABASE_URL"], autocommit=True,
            options=f"-c search_path={self.schema} -c statement_timeout=5000",
        ) as restarted:
            self.assertEqual(relevance.get_current_assessment(restarted, candidate)["status"], "excluded")
            self.assertEqual(relevance.get_current_assessment(
                restarted, candidate, purpose="question", owner_telegram_id=41, question=question,
            )["status"], "eligible")
            self.assertIsNone(relevance.get_current_assessment(
                restarted, candidate, purpose="question", owner_telegram_id=42, question=question,
            ))
            public_count = restarted.execute(
                "SELECT count(*) FROM materials m WHERE " + relevance.reader_condition("m"),
                (str(relevance.load_profile()["version"]),),
            ).fetchone()[0]
            self.assertEqual(public_count, 0)

    def test_eligible_decision_survives_a_new_connection(self):
        import psycopg
        candidate = self.candidate()
        decision = relevance.assess(candidate, review=complete_review(candidate))
        self.assertEqual(decision["status"], "eligible")
        relevance.store_assessment(self.conn, candidate, decision)
        with psycopg.connect(
            os.environ["TEST_DATABASE_URL"], autocommit=True,
            options=f"-c search_path={self.schema} -c statement_timeout=5000",
        ) as restarted:
            persisted = relevance.get_current_assessment(restarted, candidate)
        self.assertEqual(persisted["status"], "eligible")
        self.assertEqual(persisted["text_version"], decision["text_version"])
        self.assertEqual(persisted["profile_version"], decision["profile_version"])
        self.assertEqual(persisted["scope_key"], decision["scope_key"])

    def test_identical_decision_is_upserted_without_duplicate_records(self):
        candidate = self.candidate()
        decision = relevance.assess(candidate, review=complete_review(candidate))
        relevance.store_assessment(self.conn, candidate, decision)
        relevance.store_assessment(self.conn, candidate, decision)
        count = self.conn.execute(
            "SELECT count(*) FROM material_assessments WHERE material_id=%s", (candidate["id"],),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_question_admission_never_replaces_reader_or_another_owner_scope(self):
        candidate = self.candidate()
        reader = relevance.assess(candidate)
        self.assertNotEqual(reader["status"], "eligible")
        question = "Что известно о ретиноле?"
        owner_result = relevance.assess(
            candidate, purpose="question", owner_telegram_id=41, question=question,
            review=complete_review(candidate),
        )
        self.assertEqual(owner_result["status"], "eligible")
        relevance.store_assessment(self.conn, candidate, reader)
        relevance.store_assessment(self.conn, candidate, owner_result)
        self.assertNotEqual(relevance.get_current_assessment(self.conn, candidate)["status"], "eligible")
        self.assertEqual(relevance.get_current_assessment(
            self.conn, candidate, purpose="question", owner_telegram_id=41, question=question,
        )["status"], "eligible")
        self.assertIsNone(relevance.get_current_assessment(
            self.conn, candidate, purpose="question", owner_telegram_id=42, question=question,
        ))

    def test_changed_material_text_does_not_reuse_an_earlier_approval(self):
        candidate = self.candidate()
        relevance.store_assessment(
            self.conn, candidate, relevance.assess(candidate, review=complete_review(candidate)),
        )
        changed = dict(candidate, text=candidate["text"] + " An updated synthetic paragraph.")
        self.conn.execute("UPDATE materials SET text=%s WHERE id=%s", (changed["text"], candidate["id"]))
        self.assertIsNone(relevance.get_current_assessment(self.conn, changed))
        count = self.conn.execute(
            "SELECT count(*) FROM material_assessments WHERE material_id=%s", (candidate["id"],),
        ).fetchone()[0]
        self.assertEqual(count, 1, "Old decision must be retained, not silently rewritten as current")

    def test_assessment_completed_after_a_text_change_is_not_published(self):
        candidate = self.candidate()
        earlier = relevance.assess(candidate, review=complete_review(candidate))
        changed = dict(candidate, text=candidate["text"] + " The text changed during a review.")
        self.conn.execute("UPDATE materials SET text=%s WHERE id=%s", (changed["text"], candidate["id"]))
        self.assertFalse(relevance.store_assessment(self.conn, candidate, earlier))
        self.assertIsNone(relevance.get_current_assessment(self.conn, changed))

    def test_changed_reader_profile_requires_a_new_assessment(self):
        candidate = self.candidate()
        relevance.store_assessment(
            self.conn, candidate, relevance.assess(candidate, review=complete_review(candidate)),
        )
        changed_profile = copy.deepcopy(relevance.load_profile())
        changed_profile["version"] = str(changed_profile["version"]) + "-next-fixture-profile"
        with patch.object(relevance, "load_profile", return_value=changed_profile):
            self.assertIsNone(relevance.get_current_assessment(self.conn, candidate))

    def test_reapplying_additive_migration_preserves_legacy_records_and_money(self):
        candidate = self.candidate()
        mid = candidate["id"]
        self.conn.execute("INSERT INTO participants(telegram_id,role) VALUES (77701,'owner')")
        self.conn.execute("INSERT INTO saved_materials(owner_telegram_id,material_id) VALUES (77701,%s)", (mid,))
        self.conn.execute(
            "INSERT INTO findings(material_id,summary,model,prompt_version,verified) VALUES (%s,'Legacy','fixture','legacy',TRUE)", (mid,),
        )
        self.conn.execute(
            "INSERT INTO facts(material_id,statement,quote) VALUES (%s,'Legacy fixture statement',%s)",
            (mid, candidate["text"]),
        )
        self.conn.execute(
            "INSERT INTO answers(question,content,owner_telegram_id) VALUES ('Legacy fixture question','{}',77701)",
        )
        self.conn.execute(
            "INSERT INTO model_calls(stage,model,reserved_usd,actual_usd) VALUES ('fixture','fixture',1,0.25)",
        )
        self.conn.execute(
            """INSERT INTO search_calls(scope_id,purpose,operation,reserved_usd,status)
               VALUES ('fixture-reader','question','search',0.01,'unknown')""",
        )
        names = ("materials", "findings", "facts", "saved_materials", "answers", "model_calls", "search_calls")
        before_counts = {name: self.conn.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in names}
        before_text = self.conn.execute("SELECT title,text,status FROM materials WHERE id=%s", (mid,)).fetchone()
        before_money = self.conn.execute("SELECT reserved_usd,actual_usd FROM model_calls").fetchall()
        before_search = self.conn.execute("SELECT reserved_usd,actual_usd,status FROM search_calls").fetchall()
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        self.assertEqual({name: self.conn.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in names}, before_counts)
        self.assertEqual(self.conn.execute("SELECT title,text,status FROM materials WHERE id=%s", (mid,)).fetchone(), before_text)
        self.assertEqual(self.conn.execute("SELECT reserved_usd,actual_usd FROM model_calls").fetchall(), before_money)
        self.assertEqual(self.conn.execute("SELECT reserved_usd,actual_usd,status FROM search_calls").fetchall(), before_search)
        self.assertIsNone(relevance.get_current_assessment(self.conn, candidate))


if __name__ == "__main__":
    unittest.main()
