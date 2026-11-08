"""Small public API/RSS adapters. No browser scraping or private endpoints."""

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

from net import fetch, safe_error, SourceError, ERROR_REASONS


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = PlainText()
    parser.feed(value or "")
    return re.sub(r"[ \t]+", " ", "".join(parser.parts)).strip()


def xml_text(element):
    return " ".join(element.itertext()).strip() if element is not None else ""


def date_value(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).isoformat()
        except ValueError:
            return None


def request_json(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    return json.loads(fetch(url))


def rss(source, query=None):
    root = ET.fromstring(fetch(source["url"]))
    entries = root.findall(".//item")
    if entries:
        for item in entries:
            url = (item.findtext("link") or "").strip()
            if not url:
                continue
            content = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            text = plain_text(content or item.findtext("description") or "")
            yield {"url": url, "title": plain_text(item.findtext("title") or url),
                   "text": text, "published_at": date_value(item.findtext("pubDate")),
                   "authors": item.findtext("author") or item.findtext("{http://purl.org/dc/elements/1.1/}creator"),
                   "text_scope": "excerpt"}
        return
    ns = "{http://www.w3.org/2005/Atom}"
    for item in root.findall(f"{ns}entry"):
        links = item.findall(f"{ns}link")
        link = next((x for x in links if x.get("rel", "alternate") == "alternate"), None)
        if link is None or not link.get("href"):
            continue
        content = item.find(f"{ns}content")
        if content is None:
            content = item.find(f"{ns}summary")
        yield {"url": link.get("href"), "title": xml_text(item.find(f"{ns}title")),
               "text": plain_text(xml_text(content)), "text_scope": "excerpt",
               "published_at": date_value(item.findtext(f"{ns}published") or item.findtext(f"{ns}updated")),
               "authors": xml_text(item.find(f"{ns}author"))}


def europe_pmc(source, query=None):
    cfg = source["config"]
    data = request_json(source["url"], {"query": query or cfg["query"], "format": "json",
                        "resultType": "core", "pageSize": cfg.get("limit", 20), "sort": "FIRST_PDATE_D desc"})
    for row in data.get("resultList", {}).get("result", []):
        pmid = row.get("id") if row.get("source") == "MED" else None
        item = {"url": f"https://europepmc.org/article/{row['source']}/{row['id']}",
                "title": row.get("title", ""), "text": plain_text(row.get("abstractText", "")),
                "text_scope": "abstract" if row.get("abstractText") else "metadata", "doi": row.get("doi"), "pmid": pmid,
                "authors": row.get("authorString"), "published_at": date_value(row.get("firstPublicationDate"))}
        if cfg.get("full_text", True) and row.get("pmcid") and row.get("isOpenAccess") == "Y":
            try:
                full = ET.fromstring(fetch(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{row['pmcid']}/fullTextXML"))
                body = full.find("body")
                if body is not None:
                    item["text"] = "\n".join(xml_text(full.find(section)) for section in ("front", "body", "back"))
                    item["text_scope"] = "full_text"
            except Exception:
                # The available abstract is still usable and explicitly labelled.
                pass
        yield item


def pubmed(source, query=None):
    cfg = source["config"]
    args = {"db": "pubmed", "term": query or cfg["query"], "retmode": "json", "sort": "pub date",
            "retmax": cfg.get("limit", 20), "tool": "skin-research-intelligence"}
    if os.environ.get("NCBI_API_KEY"):
        args["api_key"] = os.environ["NCBI_API_KEY"]
    data = request_json(source["url"].rstrip("/") + "/esearch.fcgi", args)
    ids = data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return
    args = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml", "tool": "skin-research-intelligence"}
    if os.environ.get("NCBI_API_KEY"):
        args["api_key"] = os.environ["NCBI_API_KEY"]
    root = ET.fromstring(fetch(source["url"].rstrip("/") + "/efetch.fcgi?" + urlencode(args)))
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//MedlineCitation/PMID")
        if not pmid:
            continue
        doi = next((x.text for x in article.findall(".//ArticleId") if x.get("IdType") == "doi"), None)
        abstract = "\n".join((x.get("Label", "") + ": " if x.get("Label") else "") + xml_text(x)
                             for x in article.findall(".//Abstract/AbstractText"))
        pub = article.find(".//ArticleDate")
        published = None
        if pub is not None:
            published = date_value(f"{pub.findtext('Year')}-{pub.findtext('Month', '01')}-{pub.findtext('Day', '01')}")
        authors = "; ".join(" ".join(filter(None, (x.findtext("ForeName"), x.findtext("LastName"),
                                                   x.findtext("CollectiveName")))) for x in article.findall(".//AuthorList/Author"))
        yield {"url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "title": xml_text(article.find(".//ArticleTitle")),
               "text": abstract, "doi": doi, "pmid": pmid, "authors": authors,
               "text_scope": "abstract" if abstract else "metadata", "published_at": published}


def crossref(source, query=None):
    cfg = source["config"]
    data = request_json(source["url"], {"query": query or cfg["query"], "rows": cfg.get("limit", 20),
                                        "sort": "published", "order": "desc"})
    for row in data.get("message", {}).get("items", []):
        doi = row.get("DOI")
        if not doi:
            continue
        abstract = plain_text(row.get("abstract", ""))
        published_parts = row.get("published", {}).get("date-parts", [[]])[0]
        published = None
        if published_parts:
            parts = (published_parts + [1, 1])[:3]
            published = date_value(f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}")
        yield {"url": row.get("URL") or f"https://doi.org/{doi}", "doi": doi,
               "title": " ".join(row.get("title", [])), "text": abstract,
               "text_scope": "abstract" if abstract else "metadata",
               "authors": "; ".join(" ".join(filter(None, [a.get("given"), a.get("family")])) for a in row.get("author", [])),
               "published_at": published}


def trials(source, query=None):
    cfg = source["config"]
    data = request_json(source["url"], {"query.term": query or cfg["query"], "format": "json", "pageSize": cfg.get("limit", 20), "sort": "LastUpdatePostDate:desc"})
    for row in data.get("studies", []):
        protocol = row.get("protocolSection", {})
        identity = protocol.get("identificationModule", {})
        nct = identity.get("nctId")
        if nct:
            yield {"url": f"https://clinicaltrials.gov/study/{nct}",
                   "title": identity.get("officialTitle") or identity.get("briefTitle", nct),
                   "text": json.dumps(row, ensure_ascii=False), "text_scope": "full_text",
                   "published_at": date_value(protocol.get("statusModule", {}).get("lastUpdatePostDateStruct", {}).get("date"))}


def openfda(source, query=None):
    cfg = source["config"]
    # Endpoint query grammar differs from biomedical search; use configured query.
    term = cfg.get("query", "device.generic_name:laser")
    if query:
        term = 'device.brand_name:"' + query.replace('"', '').replace('\\', '') + '"'
    args = {"search": term, "limit": cfg.get("limit", 20), "sort": "date_received:desc"}
    if os.environ.get("OPENFDA_API_KEY"):
        args["api_key"] = os.environ["OPENFDA_API_KEY"]
    try:
        data = request_json(source["url"], args)
    except SourceError as error:
        if error.safe_code=='empty_result':
            return
        raise
    for row in data.get("results", []):
        record = str(row.get("mdr_report_key") or row.get("recall_number") or "")
        if not record:
            continue
        yield {"url": f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/detail.cfm?mdrfoi__id={record}",
               "title": f"FDA device report {record}", "text": json.dumps(row, ensure_ascii=False),
               "text_scope": "full_text"}


def beauty_facts(source, query=None):
    cfg = source["config"]
    data = request_json(source["url"], {"search_terms": query or cfg.get("query", "skin"),
                        "search_simple": 1, "action": "process", "json": 1, "page_size": cfg.get("limit", 20)})
    for row in data.get("products", []):
        code = row.get("code")
        if not code:
            continue
        # Retain field names: ingredient lists are product data, not efficacy evidence.
        fields = {k: row.get(k) for k in ("code", "product_name", "brands", "ingredients_text", "countries", "last_modified_t")}
        yield {"url": row.get("url") or f"https://world.openbeautyfacts.org/product/{code}",
               "title": row.get("product_name") or code, "text": json.dumps(fields, ensure_ascii=False), "text_scope": "full_text"}


def mfds(source, query=None):
    cfg = source["config"]
    key = os.environ.get("MFDS_API_KEY")
    if not key or not cfg.get("approved_endpoint"):
        raise ValueError("MFDS requires API key and approved_endpoint from registered documentation")
    data = request_json(cfg["approved_endpoint"], {"serviceKey": key, "type": "json", "pageNo": 1,
                          "numOfRows": cfg.get("limit", 20), **cfg.get("params", {})})
    # Registered API response mapping must be explicitly supplied, not guessed.
    rows = data
    for key_part in cfg.get("items_path", []):
        rows = rows[key_part]
    if not isinstance(rows, list):
        raise ValueError("MFDS items_path must select a list")
    for row in rows:
        title = str(row[cfg["title_field"]])
        identity = str(row[cfg["id_field"]])
        yield {"url": source["url"].rstrip("/") + "#" + identity, "title": title,
               "text": json.dumps(row, ensure_ascii=False), "text_scope": "full_text"}


class PublicPage(PlainText):
    """Small semantic HTML reader, not a site-specific browser/parser."""
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.heading = []
        self.title = []
        self.content = []
        self.in_content = 0
        self.in_heading = False
        self.in_title = False
        self.paragraphs = 0

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            if name and attrs.get("content"):
                self.meta.setdefault(name, []).append(attrs["content"])
        if tag in {"main", "article"}:
            self.in_content += 1
        if self.in_content and tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.content.append("\n")
        if tag == "h1": self.in_heading = True
        if tag == "title": self.in_title = True
        if tag == "p": self.paragraphs += 1

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag in {"main", "article"}: self.in_content = max(0, self.in_content - 1)
        if tag == "h1": self.in_heading = False
        if tag == "title": self.in_title = False

    def handle_data(self, data):
        super().handle_data(data)
        if not self.hidden:
            if self.in_content: self.content.append(data)
            if self.in_heading: self.heading.append(data)
            if self.in_title: self.title.append(data)


def direct_pages(source, query=None):
    """Read a bounded, own-domain catalogue independent of any search provider.

    Catalogue membership limits coverage; it is not a complete site search.
    Returned HTML is an excerpt, even when the full response was downloaded.
    """
    from collector.exa import allowed_scientific_url
    cfg = source.get("config", {})
    domain = (cfg.get("domain") or urlsplit(source["url"]).hostname or "").lower()
    role = cfg.get("source_role", "reference")
    pages = cfg.get("pages", [])
    if not isinstance(pages, list):
        raise ValueError("Invalid public page catalogue")
    for entry in pages[:min(10, max(1, int(cfg.get("limit", 5))))]:
        page = {"url": entry} if isinstance(entry, str) else entry
        if not isinstance(page, dict): continue
        url = page.get("url")
        if not allowed_scientific_url(url): continue
        host = (urlsplit(url).hostname or "").lower()
        if host != domain: continue
        topics = page.get("topics", [])
        if query and topics and not any(str(t).casefold() in query.casefold() for t in topics):
            continue
        try:
            raw = fetch(url, url_policy=lambda target:
                allowed_scientific_url(target) and (urlsplit(target).hostname or "").lower() == domain)
            parser = PublicPage()
            parser.feed(raw.decode("utf-8", errors="replace"))
            meta = parser.meta
            title = " ".join(parser.heading or parser.title).strip() or page.get("title") or url
            body = re.sub(r"[ \t]+", " ", "".join(parser.content or parser.parts)).strip()
            blocked = any(marker in (title + "\n" + body[:1500]).casefold() for marker in
                ("just a moment", "access denied", "verify you are human", "enable javascript and cookies"))
            readable = len(body) >= 80 and (bool(parser.content) or parser.paragraphs > 0) and not blocked
            if readable and topics:
                # A matching catalogue label cannot turn unrelated/loading HTML into evidence.
                readable = any(str(t).casefold() in (title + "\n" + body).casefold() for t in topics)
            text = body[:50000] if readable else ""
            if query and not topics:
                terms = [w.casefold() for w in re.findall(r"[\w-]{3,}", query)]
                if terms and not any(t in (title + "\n" + text).casefold() for t in terms):
                    continue
            source.setdefault("checks", []).append({"url": url,"planned":True,"complete":True,
                "status": "success" if readable else "no_text", "stage": "read","code":None if readable else 'no_text',
                "reason": "Проверен ограниченный каталог публичных страниц" if readable else
                          "Доступный содержательный текст не получен"})
            authors = meta.get("citation_author") or meta.get("author") or []
            dates = meta.get("article:published_time") or meta.get("citation_publication_date") or []
            yield {"url": url, "title": title, "text": text,
                "text_scope": "excerpt" if readable else "metadata", "authors": "; ".join(authors) or None,
                "published_at": date_value(dates[0].replace("/", "-")) if dates else None,
                "doi": (meta.get("citation_doi") or [None])[0],
                "pmid": (meta.get("citation_pmid") or [None])[0],
                "source_role": page.get("source_role", role)}
        except Exception as error:
            # One page must not prevent the rest of this independent catalogue.
            source.setdefault("checks", []).append(dict(safe_error(error,'read'),url=url,status='failed',planned=True,complete=False))


def exa_reader(source, query=None):
    from collector.exa import read_exa
    yield from read_exa(source, query)


READERS = {"rss": rss, "europe_pmc": europe_pmc, "pubmed": pubmed, "crossref": crossref,
           "clinicaltrials": trials, "openfda": openfda, "beauty_facts": beauty_facts, "mfds": mfds}
READERS.update({"direct_pages": direct_pages, "exa": exa_reader})
