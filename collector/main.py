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
from net import safe_error, SourceError, ERROR_REASONS
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
    from collector.exa import allowed_scientific_url, question_scope, ExaUnavailable, ExaBudgetExceeded
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
        planned, complete, deferred = True, False, None
        if kind=='direct_pages':
            deferred=conn.execute('SELECT deferred_at,deferred_code,deferred_reason FROM sources WHERE id=%s',(source_id,)).fetchone()
            if deferred and deferred[0]:
                # A deferred channel is outside a later check's plan, never a
                # successful read. Avoid one persisted record every worker tick.
                if purpose=='monitoring' and not force:
                    last_event=last_checked or deferred[0]
                    if (datetime.now(timezone.utc)-last_event).total_seconds()<interval*3600:
                        continue
        try:
            reader = READERS.get(kind)
            if reader is None:
                raise SourceError('configuration_error')
            if query and kind == "rss":
                continue
            if deferred and deferred[0]:
                planned=False
                status='deferred'
                code='deferred'
                reason=ERROR_REASONS['deferred']
                items=iter(())
            elif kind=='exa':
                import os
                from decimal import Decimal,InvalidOperation
                try:
                    monthly=Decimal(os.environ.get('EXA_MONTHLY_USD','0'))
                    if not monthly.is_finite() or monthly<0: raise InvalidOperation
                except (InvalidOperation,ValueError):
                    raise SourceError('configuration_error') from None
                if monthly==0:
                    planned=False
                    status,code,reason='disabled','disabled',ERROR_REASONS['disabled']
                    items=iter(())
                else:
                    try:
                        scope_limit=Decimal(os.environ.get('EXA_SCOPE_USD','0'))
                        valid_scope=scope_limit.is_finite() and scope_limit>0
                    except (InvalidOperation,ValueError):
                        valid_scope=False
                    if not valid_scope:
                        raise SourceError('configuration_error')
                    items=reader(source,query)
            else:
                items=reader(source,query)
            for index,item in enumerate(items):
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
            if not planned:
                pass
            elif page_failures:
                status = "partial" if obtained else "failed"
            elif obtained and not readable_count:
                status = "no_text"
            elif page_no_text or readable_count < obtained:
                status = "partial"
            else:
                status = "success" if obtained else "empty"
            if planned:
                complete=not page_failures
                first_error=next((c for c in source['checks'] if c.get('status')=='failed'),None)
                code=first_error.get('code','source_unavailable') if first_error else 'no_text' if status=='no_text' else 'empty_result' if status=='empty' else None
                reason=first_error.get('reason',ERROR_REASONS['source_unavailable']) if first_error else ERROR_REASONS['no_text'] if status in {'no_text','partial'} else config.get('coverage')
                if kind=='direct_pages' and not source['checks']:
                    planned,complete,status,code,reason=False,False,'out_of_scope','out_of_scope',ERROR_REASONS['out_of_scope']
                # Only these two agreed public channels are deferred on failed
                # access. Do not change active/config or appoint a replacement.
                host=(urlsplit(url).hostname or '').lower()
                if kind=='direct_pages' and host in {'aad.org','www.aad.org','cochrane.org','www.cochrane.org'} and not readable_count and (page_failures or status=='no_text'):
                    complete=False
                    if not readable_count: status='failed'
                    conn.execute('UPDATE sources SET deferred_at=now(),deferred_code=%s,deferred_reason=%s WHERE id=%s',
                                 (code or 'source_unavailable',reason or ERROR_REASONS['source_unavailable'],source_id))
            if planned and not complete:
                errors.append(name)
            check = {"source_id":source_id,"name":name,"status":status,"stage":"collection",
                     "material_ids":channel_ids,"pages":source["checks"],"reason":reason,
                     "code":code,"planned":planned,"complete":complete}
            check['stage']=first_error.get('stage','collection') if planned and first_error else 'collection'
            if planned and first_error and 'http_status' in first_error:
                check['http_status']=first_error['http_status']
            if purpose != "question":
                conn.execute("UPDATE sources SET last_checked_at=now(),last_error=%s WHERE id=%s",
                             (reason if not complete else None, source_id))
        except Exception as error:
            # A single failing source must not prevent all remaining sources.
            info=safe_error(error,'collection')
            message=info['reason']
            if purpose != "question":
                conn.execute("UPDATE sources SET last_error=%s,last_checked_at=now() WHERE id=%s",(message,source_id))
            errors.append(name)
            db.log("collector",f"{name}: {message}")
            check = dict(info,source_id=source_id,name=name,status='partial' if obtained else 'blocked' if info['code']=='budget_exceeded' else 'failed',
                         planned=True,complete=False,material_ids=channel_ids,pages=source['checks'])
        checks.append(check)
        conn.execute("""INSERT INTO source_checks(run_id,source_id,purpose,scope_id,status,stage,reason,details,planned,complete,code)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
            (run,source_id,purpose,scope_id,check["status"],check["stage"],check.get("reason"),
             json.dumps({"material_ids":channel_ids,"pages":source["checks"],"http_status":check.get('http_status')},ensure_ascii=False),
             check['planned'],check['complete'],check.get('code')))
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
