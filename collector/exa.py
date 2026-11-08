"""Optional Exa channel; other sources never depend on its key or budget.

Official contracts read 2026-11-08:
https://exa.ai/docs/reference/search (POST /search, type=auto)
https://exa.ai/docs/reference/get-contents (POST /contents, text only)
https://exa.ai/docs/reference/pricing (auto <=10 results: $0.007/request;
Contents: $0.001/page/content type). No summaries, highlights or deep search.
Server upper bounds default to zero. Explicit price acknowledgement expires in
31 days: an old price document is not an unlimited guarantee of provider costs.
"""

import hashlib
import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import db
from net import validate_url

API_URL = "https://api.exa.ai"
PRICING_DATE = "2026-11-08"
BUDGET_LOCK = 4213  # 4207 schema, 4208 ingest, 4209 model, 4210 digest, 4212 bot.
EXCLUDED_DOMAINS = ["europepmc.org", "ncbi.nlm.nih.gov", "ebi.ac.uk/europepmc"]


class ExaUnavailable(RuntimeError):
    """Safe, controlled channel failure; never carries provider error bodies."""


class ExaBudgetExceeded(ExaUnavailable):
    pass


def _decimal(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise ExaUnavailable("Некорректная настройка расходов Exa") from None


def _limits():
    monthly = min(_decimal(os.environ.get("EXA_MONTHLY_USD", "0")), Decimal("5"))
    scope = min(_decimal(os.environ.get("EXA_SCOPE_USD", "0")), Decimal("0.05"))
    if not monthly or not scope:
        raise ExaBudgetExceeded("Платный поиск Exa выключен лимитом")
    return monthly, scope


def question_scope(query, owner_telegram_id):
    """Retry-stable, owner-specific scope. No anonymous shared question budget."""
    if type(owner_telegram_id) is not int or owner_telegram_id <= 0:
        return None
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    value = f"{month}\n{owner_telegram_id}\n{' '.join(query.split()).casefold()}"
    return "question_" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def reserve_call(*, scope_id, purpose, operation, reserved_usd, run_id=None):
    """Commit before sending; one lock guards monthly AND per-research totals.

    Use a separate connection so rollback of an answer cannot refund a request.
    run_id is an audit reference, deliberately not an FK to an outer transaction.
    """
    if not isinstance(scope_id, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", scope_id):
        raise ExaUnavailable("Не задан устойчивый контекст бюджета Exa")
    if purpose not in {"question", "daily"} or operation not in {"search", "contents"}:
        raise ExaUnavailable("Некорректный этап поиска Exa")
    amount = _decimal(reserved_usd)
    monthly_limit, scope_limit = _limits()
    if not amount or amount > scope_limit or amount > monthly_limit:
        raise ExaBudgetExceeded("Следующий вызов превышает лимит Exa")
    with db.connect() as ledger, ledger.transaction():
        ledger.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_LOCK,))
        monthly = ledger.execute("""SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0)
            FROM search_calls WHERE created_at >=
              (date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')""").fetchone()[0]
        scoped = ledger.execute("""SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0)
            FROM search_calls WHERE scope_id=%s""", (scope_id,)).fetchone()[0]
        if _decimal(monthly) + amount > monthly_limit or _decimal(scoped) + amount > scope_limit:
            raise ExaBudgetExceeded("Месячный или разовый лимит Exa исчерпан")
        return ledger.execute("""INSERT INTO search_calls
            (scope_id,purpose,operation,reserved_usd,status,run_id)
            VALUES (%s,%s,%s,%s,'reserved',%s) RETURNING id""",
            (scope_id, purpose, operation, amount, run_id)).fetchone()[0]


def finish_call(call_id, actual_usd=None, status="completed"):
    """Unknown cost keeps its full reservation, including timeouts and failures."""
    if status not in {"completed", "failed", "unknown"}:
        raise ExaUnavailable("Некорректное состояние вызова Exa")
    actual = _decimal(actual_usd) if actual_usd is not None else None
    with db.connect() as ledger, ledger.transaction():
        ledger.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_LOCK,))
        ledger.execute("""UPDATE search_calls SET actual_usd=COALESCE(actual_usd,%s),
            status=%s,finished_at=now() WHERE id=%s""", (actual, status, call_id))


def allowed_scientific_url(url):
    """Policy check before paid content retrieval; no old biomedical targets."""
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            return False
        if parts.port not in {None, 80, 443}:
            return False
        if any(host == domain or host.endswith("." + domain)
               for domain in ("europepmc.org", "ncbi.nlm.nih.gov")):
            return False
        if host.endswith("ebi.ac.uk") and "/europepmc" in parts.path.lower():
            return False
        if host == "api.fda.gov" and parts.path.startswith("/device/"):
            return False
        if host == "openbeautyfacts.org" or host.endswith(".openbeautyfacts.org"):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return host not in {"localhost", "localhost.localdomain"}
    except ValueError:
        return False


def source_role_for_url(url):
    """Known public reference origins retain their role even if found by Exa."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if host in {"dermnetnz.org", "www.dermnetnz.org"} and parts.path.startswith("/topics/"):
            return "reference"
        if host in {"aad.org", "www.aad.org"} and parts.path.startswith("/public/"):
            return "professional_information"
        if host in {"cochrane.org", "www.cochrane.org"} and parts.path.startswith("/evidence/"):
            return "systematic_review_summary"
    except (ValueError, TypeError):
        pass
    return "research_candidate"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            fp.close()
        except Exception:
            pass
        raise ExaUnavailable("Перенаправление API Exa запрещено")


def post_json(path, payload):
    """Fixed API origin; key only in headers, no redirects or raw error logging."""
    if path not in {"/search", "/contents"}:
        raise ExaUnavailable("Неизвестный endpoint Exa")
    key = os.environ.get("EXA_API_KEY", "").strip()
    if not key:
        raise ExaUnavailable("Ключ Exa не настроен")
    try:
        validate_url(API_URL + path)
        request = urllib.request.Request(API_URL + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-api-key": key, "Content-Type": "application/json"}, method="POST")
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=30) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ExaUnavailable("Ответ Exa превышает лимит размера")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ExaUnavailable("Некорректный ответ Exa")
        return result
    except urllib.error.HTTPError as error:
        # Only the numeric HTTP code is safe; body/reason may echo key or query.
        code = error.code
        try:
            error.close()
        except Exception:
            # Cleanup failures must not expose an untrusted response or its body.
            pass
        raise ExaUnavailable("Ошибка HTTP Exa: " + str(code)) from None
    except ExaUnavailable:
        raise
    except Exception:
        raise ExaUnavailable("Не удалось получить ответ Exa") from None


def _settings():
    if not os.environ.get("EXA_API_KEY", "").strip():
        raise ExaUnavailable("Ключ Exa не настроен")
    _limits()
    reviewed = date.fromisoformat(PRICING_DATE)
    if (os.environ.get("EXA_PRICING_DATE") != PRICING_DATE
            or not reviewed <= datetime.now(timezone.utc).date() <= reviewed + timedelta(days=31)):
        raise ExaUnavailable("Верхняя стоимость Exa не подтверждена актуальным тарифом")
    search_upper = _decimal(os.environ.get("EXA_SEARCH_UPPER_USD", "0"))
    contents_upper = _decimal(os.environ.get("EXA_CONTENTS_UPPER_USD", "0"))
    try:
        results = min(5, max(1, int(os.environ.get("EXA_MAX_RESULTS", "5"))))
        pages = min(3, results, max(1, int(os.environ.get("EXA_MAX_CONTENTS", "3"))))
    except (ValueError, TypeError):
        raise ExaUnavailable("Некорректные ограничения страниц Exa") from None
    if search_upper < Decimal("0.007") or contents_upper < Decimal("0.001") * pages:
        raise ExaUnavailable("Не задана достаточная верхняя стоимость вызовов Exa")
    return results, pages, search_upper, contents_upper


def _paid_call(context, operation, payload, upper):
    call_id = reserve_call(scope_id=context.get("scope_id"), purpose=context.get("purpose"),
        operation=operation, reserved_usd=upper, run_id=context.get("run_id"))
    try:
        response = post_json("/" + operation, payload)
        reported = response.get("costDollars", {}).get("total")
        actual = _decimal(reported) if reported is not None else None
        finish_call(call_id, actual_usd=actual)
        if actual is not None and actual > upper:
            raise ExaUnavailable("Подтверждённая стоимость Exa превысила резерв")
        return response
    except Exception:
        # A possibly billed call is never refunded because its response was lost.
        finish_call(call_id, status="unknown")
        raise ExaUnavailable("Поиск или получение текста Exa не завершены") from None


def read_exa(source, query=None):
    """Search and bounded text retrieval; highlights/summaries are never evidence."""
    from collector.adapters import date_value, plain_text
    results_limit, pages_limit, search_upper, contents_upper = _settings()
    context = source.get("context", {})
    if (not isinstance(context, dict) or not isinstance(context.get("scope_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", context["scope_id"])
            or context.get("purpose") not in {"question", "daily"}):
        raise ExaUnavailable("Не задан устойчивый контекст бюджета Exa")
    if not isinstance(query, str) or not query.strip():
        raise ExaUnavailable("Не задана тема поиска Exa")
    results = _paid_call(context, "search", {"query": query[:1000], "type": "auto",
        "numResults": results_limit, "excludeDomains": EXCLUDED_DOMAINS,
        "contents": {"text": False, "highlights": False}}, search_upper)
    candidates = []
    seen = set()
    for row in results.get("results", []):
        url = row.get("url") if isinstance(row, dict) else None
        if allowed_scientific_url(url) and url not in seen:
            candidates.append(row)
            seen.add(url)
        if len(candidates) >= results_limit:
            break
    details = {}
    if candidates:
        urls = [row["url"] for row in candidates[:pages_limit]]
        try:
            response = _paid_call(context, "contents", {"urls": urls,
                "text": {"maxCharacters": 12000}, "highlights": False}, contents_upper)
            for row in response.get("results", []):
                if isinstance(row, dict) and row.get("url") in urls and allowed_scientific_url(row["url"]):
                    details[row["url"]] = row
        except ExaUnavailable as error:
            source.setdefault("checks", []).append({"status": "failed", "stage": "contents",
                "reason": str(error)})
    for candidate in candidates:
        row = dict(candidate)
        row.update(details.get(row["url"], {}))
        # Search-only fields do not establish a read. Contents text is required.
        content = details.get(row["url"], {}).get("text")
        text = plain_text(content)[:12000] if isinstance(content, str) else ""
        yield {"url": row["url"], "title": str(row.get("title") or row["url"]),
            "text": text, "text_scope": "excerpt" if text else "metadata",
            "authors": row.get("author"), "published_at": date_value(row.get("publishedDate")),
            "doi": row.get("doi"), "pmid": row.get("pmid"), "source_role": source_role_for_url(row["url"])}
