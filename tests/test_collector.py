"""Offline boundary tests for source parsing and document preservation."""

import base64
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from collector import adapters
from collector.main import canonical_url, ingest, ingest_manual, normalize_doi,collect


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if "SELECT id,text_scope" in sql:
            return Result(self.existing)
        if "RETURNING id" in sql:
            return Result([(1,)])
        return Result([])


class CollectorTests(unittest.TestCase):
    def test_rss_preserves_long_text_and_does_not_claim_full_text(self):
        text = "a" * 5000
        xml = f"<rss><channel><item><link>https://example.org/a</link><title>A</title><description>{text}</description></item></channel></rss>"
        with patch.object(adapters, "fetch", return_value=xml.encode()):
            row = list(adapters.rss({"url": "https://example.org/feed"}))[0]
        self.assertEqual(row["text"], text)
        self.assertEqual(row["text_scope"], "excerpt")

    def test_atom_summary_fallback_and_link(self):
        xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="https://example.org/a"/><summary>Contents</summary></entry></feed>'
        with patch.object(adapters, "fetch", return_value=xml.encode()):
            row = list(adapters.rss({"url": "https://example.org/feed"}))[0]
        self.assertEqual(row["text"], "Contents")

    def test_epmc_falls_back_to_labelled_abstract(self):
        data = {"resultList": {"result": [{"source":"MED", "id":"123", "title":"A", "abstractText":"Abstract", "pmcid":"PMC123", "isOpenAccess":"Y"}]}}
        with patch.object(adapters, "request_json", return_value=data), patch.object(adapters, "fetch", side_effect=OSError("unavailable")):
            row = list(adapters.europe_pmc({"url":"https://example.org/search", "config":{"query":"skin"}}))[0]
        self.assertEqual(row["text_scope"], "abstract")
        self.assertEqual(row["text"], "Abstract")

    def test_normalize_identifiers_and_tracking(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1234/ABC"), "10.1234/abc")
        self.assertEqual(canonical_url("https://example.org/a?utm_source=x&id=2"), "https://example.org/a?id=2")
        with self.assertRaises(ValueError):
            normalize_doi("not-a-doi")
        with self.assertRaises(ValueError):
            canonical_url("file:///etc/passwd")

    def test_shorter_scope_never_overwrites_full_text(self):
        conn = Connection([(7,"full_text","oldhash","Long full text",None,None)])
        result = ingest(conn, {"url":"https://example.org/a", "title":"A", "text":"Abstract", "text_scope":"abstract"}, 2)
        self.assertEqual(result,7)
        self.assertFalse(any("SET title=" in sql for sql,args in conn.calls))
        self.assertTrue(any("INSERT INTO material_sources" in sql for sql,args in conn.calls))

    def test_full_text_upgrade_requeues_material(self):
        conn = Connection([(7,"abstract","oldhash","Abstract",None,None)])
        ingest(conn, {"url":"https://example.org/a", "title":"A", "text":"Full text", "text_scope":"full_text"}, 2)
        self.assertTrue(any("status='new'" in sql for sql,args in conn.calls))

    def test_conflicting_identities_are_not_destructively_merged(self):
        conn = Connection([(7,"abstract","x","A",None,None),(8,"abstract","y","B",None,None)])
        with self.assertRaisesRegex(ValueError,"Conflicting"):
            ingest(conn,{"url":"https://example.org/a","text":"A","doi":"10.1234/a"},2)
        self.assertFalse(any("DELETE" in sql for sql,args in conn.calls))

    def test_manual_requires_readable_content(self):
        with self.assertRaises(ValueError):
            ingest_manual(Connection(), {"text":" "})
        with self.assertRaises(ValueError):
            ingest_manual(Connection(), {"pdf_base64":base64.b64encode(b"not a pdf").decode()})

    def test_manual_url_uses_guarded_fetch(self):
        with patch("collector.main.fetch", side_effect=ValueError("Private address")) as fetch:
            with self.assertRaisesRegex(ValueError,"Private"):
                ingest_manual(Connection(), {"url":"http://127.0.0.1/secrets"})
        fetch.assert_called_once()

    def test_failed_source_waits_before_retry_and_other_sources_continue(self):
        from datetime import datetime,timezone
        from unittest.mock import Mock
        class SourcesDB:
            def __init__(self): self.checked=None; self.error=None
            def execute(self,sql,args=()):
                if 'FROM sources WHERE active' in sql:
                    return Result([(1,'Unavailable','rss','https://example.org/feed',{},24,self.checked,self.error),
                                   (2,'Working','rss','https://example.org/ok',{},24,None,None)])
                if 'SET last_error=%s,last_checked_at=now()' in sql:
                    self.error=args[0]; self.checked=datetime.now(timezone.utc)
                return Result([])
        conn=SourcesDB()
        attempts=[]
        def reader(source,query):
            attempts.append(source['name'])
            if source['id']==1: raise TimeoutError()
            return iter([])
        with patch('collector.main.seed_sources'),patch('collector.main.db.start_run',return_value=1),patch('collector.main.db.finish_run'),patch('collector.main.db.log'),patch.dict('collector.main.READERS',{'rss':reader}):
            first=collect(conn)
            collect(conn)
        self.assertEqual(first['failed_sources'],['Unavailable'])
        self.assertEqual(attempts.count('Unavailable'),1)
        self.assertEqual(attempts.count('Working'),2)


if __name__ == "__main__":
    unittest.main()
