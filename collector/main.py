"""Scheduled collection and provenance-preserving ingestion."""

import base64
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit, parse_qsl, urlencode

import db
from net import fetch
from collector.adapters import READERS, plain_text


SOURCES = Path(__file__).resolve().parent.parent / "sources.json"
SCOPES = {"metadata": 0, "excerpt": 1, "abstract": 2, "full_text": 3}


def normalize_doi(value):
    if not value:
        return None
    value = unquote(str(value)).strip().lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
        raise ValueError("Invalid DOI")
    return value


def canonical_url(value):
    parts = urlsplit(value or "")
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("A public HTTP(S) URL is required")
    # Keep fragments: ingredient registries can use fragment identifiers.
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), parts.fragment))


def seed_sources(conn):
    """Insert defaults once; never overwrite administrator settings on restart."""
    for source in json.loads(SOURCES.read_text(encoding="utf-8")):
        conn.execute("""INSERT INTO sources (name,kind,url,active,config,interval_hours)
                        VALUES (%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(name) DO NOTHING""",
                     (source["name"], source["kind"], source["url"], source["active"],
                      json.dumps(source["config"], ensure_ascii=False), source["interval_hours"]))


def manual_source(conn):
    return conn.execute("""INSERT INTO sources(name,kind,url,active,config,interval_hours)
             VALUES('Manual input','manual','https://manual.invalid/',false,'{}',24)
             ON CONFLICT(name) DO UPDATE SET name=EXCLUDED.name RETURNING id""").fetchone()[0]


def ingest(conn, item, source_id=None, run_id=None):
    """Upsert document by DOI/PMID/URL, retain every source occurrence.

    Returns the canonical material ID. An inferior excerpt never replaces a full
    text; richer or changed same-scope text resets status to new for processing.
    Conflicting pre-existing DOI/PMID identities fail visibly instead of deleting
    documents and their derived evidence.
    """
    url = canonical_url(item["url"])
    from collector.exa import source_role_for_url
    inferred_role = source_role_for_url(url)
    source_role = inferred_role if inferred_role != "research_candidate" else item.get("source_role", "unknown")
    title = str(item.get("title") or url).strip()
    text = str(item.get("text") or "").strip()
    scope = item.get("text_scope", "excerpt")
    if scope not in SCOPES:
        raise ValueError("Unknown text scope")
    doi = normalize_doi(item.get("doi"))
    pmid = str(item["pmid"]).strip() if item.get("pmid") else None
    if pmid and not pmid.isdigit():
        raise ValueError("Invalid PMID")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(4208)")
        source_id = source_id or manual_source(conn)
        matches = conn.execute("""SELECT id,text_scope,content_hash,text,doi,pmid FROM materials
          WHERE url=%s OR doi=%s OR pmid=%s FOR UPDATE""",
          (url, doi, pmid)).fetchall()
        if len(matches) > 1:
            raise ValueError("Conflicting document identities; manual reconciliation required")
        if matches:
            material_id, old_scope, old_hash, old_text, old_doi, old_pmid = matches[0]
            if (old_doi and doi and old_doi != doi) or (old_pmid and pmid and old_pmid != pmid):
                raise ValueError("Source URL points to conflicting publication identifiers")
            better = bool(text) and (SCOPES[scope] > SCOPES.get(old_scope, -1) or
                      (scope == old_scope and digest != old_hash))
            conn.execute("""UPDATE materials SET doi=COALESCE(doi,%s),pmid=COALESCE(pmid,%s),
                           authors=COALESCE(authors,%s),source_role=CASE
                             WHEN source_role IN ('unknown','research_candidate') THEN %s ELSE source_role END
                           WHERE id=%s""",
                         (doi, pmid, item.get("authors"), source_role, material_id))
            if better:
                conn.execute("""UPDATE materials SET title=%s,text=%s,text_scope=%s,content_hash=%s,
                               status='new',attempts=0,last_error=NULL,updated_at=now() WHERE id=%s""",
                             (title, text, scope, digest, material_id))
        else:
            material_id = conn.execute("""INSERT INTO materials
              (source_id,url,title,text,published_at,doi,pmid,authors,text_scope,content_hash,run_id,source_role)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
              (source_id,url,title,text,item.get("published_at"),doi,pmid,item.get("authors"),scope,digest,
               run_id,source_role)).fetchone()[0]
        conn.execute("""INSERT INTO material_sources(material_id,source_id,url,source_role)
                        VALUES(%s,%s,%s,%s) ON CONFLICT(material_id,source_id,url) DO UPDATE
                        SET source_role=CASE WHEN material_sources.source_role IN ('unknown','research_candidate')
                          THEN EXCLUDED.source_role ELSE material_sources.source_role END""",
                     (material_id,source_id,url,item.get("source_role",source_role)))
        return material_id


def ingest_manual(conn, payload):
    """Accept text, public URL, or a bounded base64 PDF; retain extracted text only."""
    if not isinstance(payload, dict):
        raise ValueError("Expected an object")
    text = payload.get("text") or ""
    url = payload.get("url")
    scope = "full_text"
    if payload.get("pdf_base64"):
        encoded = payload["pdf_base64"]
        if not isinstance(encoded, str) or len(encoded) > 16 * 1024 * 1024:
            raise ValueError("PDF exceeds 12 MB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid PDF base64") from error
        if not data.startswith(b"%PDF-"):
            raise ValueError("Expected a PDF document")
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted or len(reader.pages) > 300:
                raise ValueError("PDF must be unencrypted and contain at most 300 pages")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as error:
            raise ValueError("Cannot extract PDF text") from error
    elif url and not text:
        data = fetch(canonical_url(url))
        text = plain_text(data.decode("utf-8", errors="replace"))
        scope = "excerpt"  # HTML extraction does not establish full-text completeness.
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Provide readable text, URL or a PDF with a text layer")
    if len(text) > 2_000_000:
        raise ValueError("Extracted text exceeds 2 million characters")
    if not url:
        identity = hashlib.sha256(text.encode("utf-8")).hexdigest()
        url = f"https://manual.invalid/document/{identity}"
    return ingest(conn, {"url": url, "title": payload.get("title") or "Добавленный материал",
                   "text": text, "text_scope": scope, "doi": payload.get("doi"),
                   "pmid": payload.get("pmid"), "authors": payload.get("authors")})


def collect(conn, force=False, query=None, *, purpose=None, scope_id=None, owner_telegram_id=None):
    """Collect active sources that are due; query bypasses schedule, not activation."""
    seed_sources(conn)
    run = db.start_run(conn, "collector")
    from collector.exa import allowed_scientific_url, question_scope, ExaUnavailable
    purpose = purpose or ("question" if query else "monitoring")
    if purpose not in {"question", "monitoring", "daily"}:
        raise ValueError("Unknown collection purpose")
    if purpose == "question" and scope_id is None and query:
        scope_id = question_scope(query, owner_telegram_id)
    count, errors, material_ids, checks = 0, [], [], []
    rows = conn.execute("""SELECT id,name,kind,url,config,interval_hours,last_checked_at,last_error
       FROM sources WHERE active AND kind <> 'manual' ORDER BY id""").fetchall()
    for source_id,name,kind,url,config,interval,last_checked,last_error in rows:
        # Runtime policy also applies to databases seeded with old active defaults.
        if kind in {"europe_pmc", "pubmed"} or not allowed_scientific_url(url):
            # Other historical channels continue monitoring, but not old biomedical APIs.
            if kind in {"europe_pmc", "pubmed"} or kind not in {"openfda", "beauty_facts"}:
                continue
        if purpose == "question" and kind not in {"direct_pages", "exa"}:
            continue
        if kind == "exa" and purpose == "monitoring":
            continue  # Daily paid discovery is introduced separately in DIG-001.
        if not force and not query and last_checked:
            wait_hours=min(interval,1) if last_error else interval
            if (datetime.now(timezone.utc) - last_checked).total_seconds() < wait_hours * 3600:
                continue
        config = json.loads(config) if isinstance(config,str) else config or {}
        source = {"id":source_id,"name":name,"kind":kind,"url":url,"config":config,"checks":[],
                  "context":{"purpose":purpose,"scope_id":scope_id,"run_id":run}}
        obtained, readable_count, channel_ids = 0, 0, []
        try:
            reader = READERS.get(kind)
            if reader is None:
                raise ValueError(f"Unsupported source kind: {kind}")
            if query and kind == "rss":
                continue
            for index,item in enumerate(reader(source,query)):
                if index >= int(config.get("limit",20)):
                    break
                if purpose == "question" and not allowed_scientific_url(item.get("url")):
                    continue
                mid = ingest(conn,item,source_id,run_id=run)
                material_ids.append(mid)
                channel_ids.append(mid)
                obtained += 1
                if item.get("text_scope") != "metadata" and str(item.get("text") or "").strip():
                    readable_count += 1
                count += 1
            page_failures = any(c.get("status") == "failed" for c in source["checks"])
            page_no_text = any(c.get("status") == "no_text" for c in source["checks"])
            if page_failures:
                errors.append(name)
            if page_failures:
                status = "partial" if obtained else "failed"
            elif obtained and not readable_count:
                status = "no_text"
            elif page_no_text or readable_count < obtained:
                status = "partial"
            else:
                status = "success" if obtained else "empty"
            check = {"source_id":source_id,"name":name,"status":status,"stage":"collection",
                     "material_ids":channel_ids,"pages":source["checks"],
                     "reason": "Доступный текст отсутствует у части материалов" if status in {"no_text","partial"}
                               else config.get("coverage")}
            if purpose != "question":
                conn.execute("UPDATE sources SET last_checked_at=now(),last_error=%s WHERE id=%s",
                             ("Часть страниц недоступна" if page_failures else None, source_id))
        except Exception as error:
            # A single failing source must not prevent all remaining sources.
            message = str(error) if isinstance(error,ExaUnavailable) else f"{type(error).__name__}: collection failed"
            if purpose != "question":
                conn.execute("UPDATE sources SET last_error=%s,last_checked_at=now() WHERE id=%s",(message,source_id))
            errors.append(name)
            db.log("collector",f"{name}: {message}")
            check = {"source_id":source_id,"name":name,"status":"partial" if obtained else "failed",
                     "stage":"collection","reason":message,"material_ids":channel_ids,"pages":source["checks"]}
        checks.append(check)
        conn.execute("""INSERT INTO source_checks(run_id,source_id,purpose,scope_id,status,stage,reason,details)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (run,source_id,purpose,scope_id,check["status"],check["stage"],check.get("reason"),
             json.dumps({"material_ids":channel_ids,"pages":source["checks"]},ensure_ascii=False)))
    note = f"Processed {count} records; failed sources: {', '.join(errors) or 'none'}"
    db.finish_run(conn,"collector",run,count,note)
    return {"processed":count,"failed_sources":errors,"material_ids":list(dict.fromkeys(material_ids)),
            "checks":checks,"scope_id":scope_id,"run_id":run}


def search(conn, query, *, scope_id=None, owner_telegram_id=None):
    """Synchronous supplemental search, preserving collected sources in the base."""
    if not isinstance(query,str) or not query.strip() or len(query)>1000:
        raise ValueError("Search query must contain 1–1000 characters")
    return collect(conn,force=True,query=query.strip(),purpose="question",scope_id=scope_id,
                   owner_telegram_id=owner_telegram_id)


def main():
    with db.connect() as conn:
        db.init_schema(conn)
        collect(conn)


if __name__ == "__main__":
    main()
