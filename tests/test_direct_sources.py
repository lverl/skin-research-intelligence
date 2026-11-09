"""Independent SRC-003 contracts; synthetic HTML, no external requests."""

import json
import os
import socket
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch
from uuid import uuid4

import net
from collector import adapters
from collector import main as collector
from test_reader_relevance import complete_review


PAGES = (
    ("DermNet", "https://dermnetnz.org/topics/topical-retinoids", "reference"),
    ("AAD", "https://www.aad.org/public/retinoid-retinol", "professional_information"),
    ("Cochrane", "https://www.cochrane.org/evidence/fixture-review", "systematic_review_summary"),
)


def source_fixture(index):
    name, url, role = PAGES[index]
    return {"id": index + 1, "name": name, "kind": "direct_pages", "url": url,
            "config": {"domain": url.split('/')[2], "source_role": role,
                       "pages": [{"url": url, "topics": ["retinol"], "source_role": role}]},
            "checks": []}


def html_fixture(index):
    name, url, role = PAGES[index]
    return (f'<html><head><title>{name} retinol fixture</title>'
            f'<meta name="citation_author" content="Fixture {name} Author">'
            '<meta name="citation_publication_date" content="2004-02-03">'
            '</head><body><main><h1>Retinol fixture</h1>'
            '<p>Synthetic fixture: adult participants received topical retinol for facial skincare with human study results.</p>'
            f'<p>Synthetic {role} text. Twelve fixture observations were recorded.</p>'
            '<p>This synthetic page is not clinical advice or a real study.</p>'
            '</main><script>HIDDEN_SCRIPT_SECRET</script></body></html>').encode()


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class SourceListDB:
    """Source selection boundary only; material ingestion is separately tested on PG."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        if "FROM sources WHERE active" in sql:
            return Result(self.rows)
        if "RETURNING id" in sql:
            return Result([(999,)])
        return Result()


class DirectSourceTests(unittest.TestCase):
    def test_three_independent_pages_keep_origin_authors_date_and_limited_scope(self):
        for index, (name, url, role) in enumerate(PAGES):
            with self.subTest(source=name), patch.dict(os.environ, {"EXA_API_KEY": ""}), \
                    patch.object(adapters, "fetch", return_value=html_fixture(index)) as fetch:
                rows = list(adapters.direct_pages(source_fixture(index), "retinol"))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["url"], url)
            self.assertEqual(row["source_role"], role)
            self.assertEqual(row["text_scope"], "excerpt")
            self.assertIn("Twelve fixture observations", row["text"])
            self.assertNotIn("HIDDEN_SCRIPT_SECRET", row["text"])
            self.assertIn(f"Fixture {name} Author", row["authors"])
            self.assertIn("2004-02-03", str(row["published_at"]))
            self.assertTrue(row["title"])
            fetch.assert_called_once()

    def test_old_relevant_article_is_not_filtered_by_publication_date(self):
        with patch.object(adapters, "fetch", return_value=html_fixture(0)):
            rows = list(adapters.direct_pages(source_fixture(0), "retinol"))
        self.assertEqual(len(rows), 1)
        self.assertIn("2004", str(rows[0]["published_at"]))

    def test_metadata_page_does_not_claim_readable_body_or_full_text(self):
        metadata = b'<html><head><title>Retinol metadata</title><meta name="author" content="Fixture Author"></head><body></body></html>'
        source = source_fixture(0)
        with patch.object(adapters, "fetch", return_value=metadata):
            rows = list(adapters.direct_pages(source, "retinol"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text_scope"], "metadata")
        self.assertEqual(rows[0]["text"], "")
        self.assertEqual(rows[0]["url"], PAGES[0][1])

    def test_foreign_page_is_never_fetched_by_direct_channel(self):
        source = source_fixture(0)
        source["config"]["pages"] = [{"url": "https://unrelated.example/retinol", "topics": ["retinol"]}]
        with patch.object(adapters, "fetch", side_effect=AssertionError("External fetch forbidden")) as fetch:
            rows = list(adapters.direct_pages(source, "retinol"))
        self.assertEqual(rows, [])
        fetch.assert_not_called()

    def test_failed_public_page_does_not_hide_another_page_or_trigger_exa(self):
        source = source_fixture(0)
        second_url = "https://dermnetnz.org/topics/fixture-second-page"
        source["config"]["pages"].append({"url": second_url, "topics": ["retinol"]})
        with patch.object(adapters, "fetch", side_effect=[TimeoutError("fixture network failure"), html_fixture(0)]), \
                patch("collector.exa.post_json", side_effect=AssertionError("Exa fallback was not authorized")) as exa_transport:
            rows = list(adapters.direct_pages(source, "retinol"))
        readable = [row for row in rows if row.get("text")]
        self.assertEqual(len(readable), 1)
        self.assertEqual(readable[0]["url"], second_url)
        self.assertTrue(source["checks"], "The failed page must leave a diagnostic check")
        exa_transport.assert_not_called()

    def test_active_legacy_apis_and_catalogue_are_not_used_for_scientific_query(self):
        sources = [(1, "Europe PMC", "europe_pmc", "https://europepmc.org", {}, 24, None, None),
                   (2, "PubMed", "pubmed", "https://pubmed.ncbi.nlm.nih.gov", {}, 24, None, None),
                   (3, "Device events", "openfda", "https://api.fda.gov/device/event.json", {}, 24, None, None),
                   (4, "Catalogue", "beauty_facts", "https://world.openbeautyfacts.org", {}, 24, None, None),
                   (5, "Working direct", "direct_pages", PAGES[0][1], source_fixture(0)["config"], 24, None, None)]
        invoked = []

        def reader(source, query):
            invoked.append(source["kind"])
            if source["kind"] != "direct_pages":
                raise AssertionError("A legacy or unrelated API was invoked")
            return iter([{"url": PAGES[0][1], "title": "Retinol", "text": "Fixture text", "text_scope": "excerpt"}])

        conn = SourceListDB(sources)
        with patch.object(collector, "seed_sources"), patch.object(collector.db, "start_run", return_value=10), \
                patch.object(collector.db, "finish_run"), patch.object(collector.db, "log"), \
                patch.dict(collector.READERS, {kind: reader for kind in ("direct_pages", "europe_pmc", "pubmed", "openfda", "beauty_facts")}), \
                patch.object(collector, "ingest", return_value=88) as ingest:
            result = collector.search(conn, "retinol", scope_id="fixture-scientific-query")
        self.assertEqual(invoked, ["direct_pages"])
        self.assertEqual(result["material_ids"], [88])
        self.assertEqual(ingest.call_count, 1)

    def test_redirect_to_forbidden_provider_stops_before_second_http_request(self):
        from collector.exa import allowed_scientific_url
        response = Mock(status=302)
        response.getheader.return_value = "https://pubmed.ncbi.nlm.nih.gov/123/"
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch.object(net.socket, "getaddrinfo", return_value=addresses), \
                patch.object(net.http.client, "HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaises(ValueError):
                net.fetch("https://dermnetnz.org/topics/topical-retinoids", url_policy=allowed_scientific_url)
        self.assertEqual(connection.call_count, 1)
        self.assertEqual(connection.return_value.request.call_count, 1)

    def test_direct_channel_redirect_to_foreign_domain_is_not_fetched(self):
        source = source_fixture(0)
        response = Mock(status=302)
        response.getheader.return_value = "https://foreign.example/retinol"
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch.object(net.socket, "getaddrinfo", return_value=addresses), \
                patch.object(net.http.client, "HTTPSConnection") as connection, \
                patch.object(adapters, "fetch", wraps=net.fetch):
            connection.return_value.getresponse.return_value = response
            rows = list(adapters.direct_pages(source, "retinol"))
        self.assertEqual(rows, [])
        self.assertEqual(connection.call_count, 1)
        self.assertEqual(connection.return_value.request.call_count, 1)
        self.assertTrue(source["checks"])

    def test_url_policy_does_not_bypass_existing_public_address_guard(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(net.socket, "getaddrinfo", return_value=private), \
                patch.object(net.http.client, "HTTPSConnection") as connection:
            with self.assertRaises(ValueError):
                net.fetch("https://dermnetnz.org/topics/topical-retinoids", url_policy=lambda url: True)
        connection.assert_not_called()

    def test_same_domain_public_redirect_is_still_usable(self):
        first = Mock(status=302)
        first.getheader.return_value = "/topics/fixture-updated"
        final = Mock(status=200)
        final.read.return_value = b"Synthetic readable public result"
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch.object(net.socket, "getaddrinfo", return_value=addresses), \
                patch.object(net.http.client, "HTTPSConnection") as connection:
            connection.return_value.getresponse.side_effect = [first, final]
            result = net.fetch("https://dermnetnz.org/topics/topical-retinoids",
                               url_policy=lambda url: url.startswith("https://dermnetnz.org/"))
        self.assertEqual(result, b"Synthetic readable public result")
        self.assertEqual(connection.return_value.request.call_count, 2)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated TEST_DATABASE_URL is required")
class DirectSourcePostgresTests(unittest.TestCase):
    """Actual importer and processing on an isolated schema, with fake HTTP/LLM."""

    def setUp(self):
        import psycopg
        import db
        self.psycopg = psycopg
        self.schema = "src003_direct_" + uuid4().hex
        self.admin = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True,
                                    options=f"-c search_path={self.schema}")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.close()

    def test_seed_is_idempotent_and_keeps_admin_configuration(self):
        from db import jsonb
        collector.seed_sources(self.conn)
        defaults = json.loads(collector.SOURCES.read_text(encoding="utf-8"))
        expected = {source["name"] for source in defaults}
        actual = {row[0] for row in self.conn.execute("SELECT name FROM sources").fetchall()}
        self.assertTrue(expected.issubset(actual))
        target = next(source["name"] for source in defaults if source["kind"] == "direct_pages")
        self.conn.execute("UPDATE sources SET active=FALSE,interval_hours=72,config=%s WHERE name=%s",
                          (jsonb({"admin_setting": "must survive"}), target))
        collector.seed_sources(self.conn)
        self.assertEqual(self.conn.execute("SELECT active,interval_hours,config FROM sources WHERE name=%s", (target,)).fetchone(),
                         (False, 72, {"admin_setting": "must survive"}))
        self.assertEqual(len(self.conn.execute("SELECT name FROM sources").fetchall()), len(actual))

    def test_schema_disables_legacy_apis_without_changing_other_admin_settings(self):
        import db
        from db import jsonb
        configured = (
            ("Legacy Europe PMC fixture", "europe_pmc", 72, {"admin_query": "keep Europe PMC"}),
            ("Legacy PubMed fixture", "pubmed", 48, {"admin_query": "keep PubMed"}),
            ("Independent RSS fixture", "rss", 36, {"admin_query": "keep independent"}),
        )
        for name, kind, interval, config in configured:
            self.conn.execute("INSERT INTO sources(name,kind,url,active,interval_hours,config) VALUES (%s,%s,%s,TRUE,%s,%s)",
                              (name, kind, "https://example.org/" + kind, interval, jsonb(config)))
        db.init_schema(self.conn)
        for name, kind, interval, config in configured:
            with self.subTest(kind=kind):
                active, actual_interval, actual_config = self.conn.execute(
                    "SELECT active,interval_hours,config FROM sources WHERE name=%s", (name,)).fetchone()
                self.assertEqual(active, kind == "rss")
                self.assertEqual(actual_interval, interval)
                self.assertEqual(actual_config, config)

    def test_three_direct_imports_become_three_verified_facts_with_exa_disabled(self):
        from db import jsonb
        from worker.main import process_material
        import llm
        for index in range(3):
            source = source_fixture(index)
            self.conn.execute("INSERT INTO sources(name,kind,url,active,config) VALUES (%s,'direct_pages',%s,TRUE,%s)",
                              (source["name"], source["url"], jsonb(source["config"])))
        fixtures = {PAGES[index][1]: html_fixture(index) for index in range(3)}

        def model(conn, stage, instructions, data):
            if stage == "reader_review":
                return complete_review({"text": data["text"]})
            if stage == "triage":
                return {"relevant": True, "important": True, "kind": "expert", "summary": "Фикстура", "topics": []}
            if stage == "extract":
                return {"facts": [{"statement": "Twelve fixture observations were recorded.",
                                    "quote": "Twelve fixture observations were recorded.", "payload": {}}], "limitations": []}
            if stage == "verify":
                return {"accepted_indices": [0], "limitations": []}
            raise AssertionError("Unexpected model stage: " + stage)

        with patch.dict(os.environ, {"EXA_API_KEY": "", "EXA_MONTHLY_USD": "0", "EXA_SCOPE_USD": "0"}), \
                patch.object(collector, "seed_sources"), patch.object(adapters, "fetch", side_effect=lambda url, **kwargs: fixtures[url]), \
                patch("collector.exa.post_json", side_effect=AssertionError("External Exa transport forbidden")), \
                patch.object(llm, "call", side_effect=model):
            result = collector.search(self.conn, "retinol", scope_id="fixture-three-direct")
            self.assertEqual(len(result["material_ids"]), 3)
            for mid in result["material_ids"]:
                row = self.conn.execute("SELECT id,title,text,text_scope FROM materials WHERE id=%s", (mid,)).fetchone()
                process_material(self.conn, row)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts WHERE active").fetchone()[0], 3)
        actual_urls = {row[0] for row in self.conn.execute("SELECT url FROM materials").fetchall()}
        self.assertEqual(actual_urls, set(fixtures))
        actual_roles = dict(self.conn.execute("SELECT url,source_role FROM materials").fetchall())
        self.assertEqual(actual_roles, {url: role for name, url, role in PAGES})

    def test_duplicate_publication_preserves_both_channels_and_richer_text(self):
        from collector.main import ingest
        source_ids = []
        for name in ("Direct fixture", "Exa fixture"):
            source_ids.append(self.conn.execute("INSERT INTO sources(name,kind,url) VALUES (%s,'direct_pages','https://example.org') RETURNING id", (name,)).fetchone()[0])
        original = {"url": "https://example.org/paper", "title": "Old fixture paper", "text": "Full fixture text", "text_scope": "full_text", "doi": "10.1234/FIXTURE", "published_at": "2004-02-03"}
        first = ingest(self.conn, original, source_ids[0])
        duplicate = {**original, "url": "https://publisher.example/paper", "text": "Short abstract", "text_scope": "abstract", "doi": "https://doi.org/10.1234/fixture"}
        second = ingest(self.conn, duplicate, source_ids[1])
        self.assertEqual(first, second)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM material_sources WHERE material_id=%s", (first,)).fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT text,text_scope FROM materials WHERE id=%s", (first,)).fetchone(), ("Full fixture text", "full_text"))

    def test_richer_exa_text_does_not_upgrade_aad_patient_material_into_research(self):
        direct_id = self.conn.execute("INSERT INTO sources(name,kind,url) VALUES ('AAD fixture','direct_pages','https://www.aad.org/public/fixture') RETURNING id").fetchone()[0]
        exa_id = self.conn.execute("INSERT INTO sources(name,kind,url) VALUES ('Exa fixture','exa','https://api.exa.ai/search') RETURNING id").fetchone()[0]
        item = {"url": "https://www.aad.org/public/fixture", "title": "AAD patient fixture",
                "text": "Original public explanation.", "text_scope": "excerpt",
                "source_role": "professional_information"}
        first = collector.ingest(self.conn, item, direct_id)
        richer = {**item, "text": "A longer public explanation with additional fixture paragraphs.",
                  "source_role": "research_candidate"}
        second = collector.ingest(self.conn, richer, exa_id)
        self.assertEqual(first, second)
        self.assertEqual(self.conn.execute("SELECT text,source_role FROM materials WHERE id=%s", (first,)).fetchone(),
                         (richer["text"], "professional_information"))
        origins = dict(self.conn.execute("SELECT source_id,source_role FROM material_sources WHERE material_id=%s", (first,)).fetchall())
        self.assertEqual(origins, {direct_id: "professional_information", exa_id: "research_candidate"})


if __name__ == "__main__":
    unittest.main()
