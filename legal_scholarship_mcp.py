#!/usr/bin/env python3
"""
legal_scholarship_mcp.py
========================
An MCP (Model Context Protocol) server that searches scholarly literature —
useful for legal and academic research — through the OpenAlex API.

It exposes five read-only tools to an MCP client (for example Claude Desktop):
  - search_scholarship  : full-text search of works, with year/sort filters
  - get_work_details    : one work's full record, with a readable abstract
  - find_citing_works   : works that CITE a given work (forward citations)
  - search_authors      : resolve an author name to OpenAlex author profiles
  - get_author_works    : a given author's works, by OpenAlex author ID

Status: personal proof-of-concept, in active development. It returns scholarly
metadata and abstracts; it does not provide full-text articles and does not give
legal advice.

Setup
-----
    pip install -r requirements.txt

OpenAlex has required a free API key since 13 February 2026 (the polite pool was
retired). Create a free account at https://openalex.org, copy your key from
https://openalex.org/settings/api, then set it in your environment:

    export OPENALEX_API_KEY="your-free-key"

A free key gives roughly $1 of usage per day — ample for interactive research.

Run (stdio transport, for local MCP clients):
    python legal_scholarship_mcp.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from enum import Enum
from typing import Annotated, Any, Optional

import httpx
from pydantic import Field
from mcp.server.fastmcp import FastMCP

# --- Module constants -------------------------------------------------------
OPENALEX_BASE = "https://api.openalex.org"
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 4
_BACKOFF_BASE = 0.5  # seconds; tests set this to 0 for speed
USER_AGENT = "legal-scholarship-mcp/0.3 (+https://github.com/your-username/legal-scholarship-mcp)"

WORK_SUMMARY_FIELDS = (
    "id,display_name,publication_year,publication_date,cited_by_count,doi,"
    "authorships,primary_location,open_access,type"
)
WORK_DETAIL_FIELDS = WORK_SUMMARY_FIELDS + ",abstract_inverted_index,referenced_works_count"
AUTHOR_FIELDS = (
    "id,display_name,works_count,cited_by_count,summary_stats,last_known_institutions,orcid"
)

mcp = FastMCP("legal_scholarship_mcp")


class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


class SortOption(str, Enum):
    """How to order search results."""
    RELEVANCE = "relevance"
    CITATIONS = "citations"
    DATE = "date"


SORT_MAP = {
    SortOption.RELEVANCE: "relevance_score:desc",
    SortOption.CITATIONS: "cited_by_count:desc",
    SortOption.DATE: "publication_date:desc",
}


# --- Shared helpers (kept DRY across tools) ---------------------------------
def _api_key() -> Optional[str]:
    return os.environ.get("OPENALEX_API_KEY")


def _missing_key_message() -> str:
    return (
        "Error: no OpenAlex API key found. OpenAlex has required a free API key "
        "since 13 February 2026. Create a free account at https://openalex.org, copy "
        "your key from https://openalex.org/settings/api, set it as the "
        "OPENALEX_API_KEY environment variable, and try again."
    )


def _handle_api_error(e: Exception) -> str:
    """Turn an exception into a clear, actionable message for the agent."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code in (401, 403):
            return f"Error: OpenAlex rejected the request ({code}). Check that OPENALEX_API_KEY is set and valid."
        if code == 404:
            return "Error: not found (404). Check the work or author identifier is correct."
        if code == 409:
            return "Error: OpenAlex daily free credits are exhausted (409). Try again tomorrow."
        if code == 429:
            return "Error: rate limit exceeded (429). Wait a moment and try again."
        return f"Error: OpenAlex request failed with status {code}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: the request to OpenAlex timed out. Please try again."
    return f"Error: unexpected problem contacting OpenAlex ({type(e).__name__})."


async def _get(path: str, params: dict[str, Any], client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """Single entry point for OpenAlex GET requests.

    Attaches the API key, sends a polite User-Agent, and retries transient
    failures (429 / 5xx / timeouts) with exponential backoff, as OpenAlex
    recommends. A client may be injected for testing.
    """
    params = {**params, "api_key": _api_key()}
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.get(f"{OPENALEX_BASE}{path}", params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise
            if resp.status_code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc:
            raise last_exc
        raise RuntimeError("request loop exited without a response")  # pragma: no cover
    finally:
        if own:
            await client.aclose()


def _reconstruct_abstract(inverted: Optional[dict[str, list[int]]]) -> Optional[str]:
    """Rebuild a plain-text abstract from OpenAlex's inverted index."""
    if not inverted:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def _short_id(openalex_id: str) -> str:
    """Turn 'https://openalex.org/W123' into 'W123'."""
    return (openalex_id or "").rstrip("/").split("/")[-1]


def _authors(work: dict[str, Any], limit: int = 6) -> str:
    names = [a.get("author", {}).get("display_name", "") for a in work.get("authorships", [])]
    names = [n for n in names if n]
    shown = ", ".join(names[:limit])
    if len(names) > limit:
        shown += " et al."
    return shown or "Unknown"


def _venue(work: dict[str, Any]) -> Optional[str]:
    source = (work.get("primary_location") or {}).get("source") or {}
    return source.get("display_name")


def _oa_url(work: dict[str, Any]) -> Optional[str]:
    return (work.get("open_access") or {}).get("oa_url")


def _work_summary_dict(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _short_id(work.get("id", "")),
        "title": work.get("display_name"),
        "year": work.get("publication_year"),
        "authors": _authors(work),
        "venue": _venue(work),
        "cited_by_count": work.get("cited_by_count"),
        "doi": work.get("doi"),
        "open_access_url": _oa_url(work),
    }


def _format_work_summaries_md(works: list[dict[str, Any]], total: int, header: str) -> str:
    if not works:
        return "No matching works found. Try broadening the query or removing the year filters."
    lines = [f"{header} — {total} total (showing {len(works)}):", ""]
    for i, w in enumerate(works, 1):
        s = _work_summary_dict(w)
        block = f"{i}. **{s['title']}** ({s['year']})\n   {s['authors']}"
        if s["venue"]:
            block += f" \u2014 {s['venue']}"
        block += f"\n   Cited by {s['cited_by_count']} \u00b7 ID: {s['id']}"
        if s["doi"]:
            block += f" \u00b7 {s['doi']}"
        lines.append(block)
    return "\n".join(lines)


def _author_summary_dict(author: dict[str, Any]) -> dict[str, Any]:
    stats = author.get("summary_stats") or {}
    insts = author.get("last_known_institutions") or []
    institution = insts[0].get("display_name") if insts else None
    return {
        "id": _short_id(author.get("id", "")),
        "name": author.get("display_name"),
        "works_count": author.get("works_count"),
        "cited_by_count": author.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "institution": institution,
        "orcid": author.get("orcid"),
    }


def _format_author_summaries_md(authors: list[dict[str, Any]], total: int) -> str:
    if not authors:
        return "No matching authors found. Try a different spelling of the name."
    lines = [f"Found {total} authors (showing {len(authors)}):", ""]
    for i, a in enumerate(authors, 1):
        s = _author_summary_dict(a)
        block = f"{i}. **{s['name']}** \u00b7 ID: {s['id']}"
        if s["institution"]:
            block += f"\n   {s['institution']}"
        block += f"\n   Works: {s['works_count']} \u00b7 Cited by: {s['cited_by_count']}"
        if s["h_index"] is not None:
            block += f" \u00b7 h-index: {s['h_index']}"
        lines.append(block)
    return "\n".join(lines)


async def _search_works(api_params: dict[str, Any], header: str, page: int, response_format: ResponseFormat) -> str:
    """Shared works-list path used by search_scholarship, find_citing_works and get_author_works."""
    api_params.setdefault("select", WORK_SUMMARY_FIELDS)
    try:
        data = await _get("/works", api_params)
    except Exception as e:  # noqa: BLE001 - converted to an actionable message
        return _handle_api_error(e)

    works = data.get("results", [])
    total = (data.get("meta") or {}).get("count", len(works))
    if response_format is ResponseFormat.JSON:
        payload = {"total": total, "count": len(works), "page": page,
                   "results": [_work_summary_dict(w) for w in works]}
        return json.dumps(payload, indent=2, ensure_ascii=False)
    return _format_work_summaries_md(works, total, header)


# --- Tools ------------------------------------------------------------------
_READONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


@mcp.tool(name="search_scholarship", annotations={"title": "Search scholarly literature (OpenAlex)", **_READONLY})
async def search_scholarship(
    query: Annotated[str, Field(description="Search terms, e.g. 'artificial intelligence copyright liability'.", min_length=1, max_length=300)],
    from_year: Annotated[Optional[int], Field(description="Earliest publication year, inclusive (e.g. 2018).", ge=1800, le=2100)] = None,
    to_year: Annotated[Optional[int], Field(description="Latest publication year, inclusive (e.g. 2025).", ge=1800, le=2100)] = None,
    max_results: Annotated[int, Field(description="How many results to return (1-100).", ge=1, le=100)] = 10,
    page: Annotated[int, Field(description="Results page, for paging beyond the first set.", ge=1)] = 1,
    sort: Annotated[SortOption, Field(description="Order by 'relevance', 'citations' (most cited first) or 'date' (newest first).")] = SortOption.RELEVANCE,
    response_format: Annotated[ResponseFormat, Field(description="'markdown' for a readable list or 'json' for structured data.")] = ResponseFormat.MARKDOWN,
) -> str:
    """Search scholarly works (papers, articles) via OpenAlex — useful for legal and academic research.

    Searches titles, abstracts and full text for the query, with optional year filtering and sorting.
    For legal research, include legal terms in the query. Use get_work_details with a returned ID for
    the full record and abstract, or find_citing_works to see who has cited a result.

    Returns (markdown): a numbered, readable list of works.
    Returns (json): {"total", "count", "page", "results":[{"id","title","year","authors","venue",
        "cited_by_count","doi","open_access_url"}, ...]}
    """
    if not _api_key():
        return _missing_key_message()

    api_params: dict[str, Any] = {"search": query, "per_page": max_results, "page": page, "sort": SORT_MAP[sort]}
    filters = []
    if from_year:
        filters.append(f"from_publication_date:{from_year}-01-01")
    if to_year:
        filters.append(f"to_publication_date:{to_year}-12-31")
    if filters:
        api_params["filter"] = ",".join(filters)
    return await _search_works(api_params, f'Results for "{query}"', page, response_format)


@mcp.tool(name="get_work_details", annotations={"title": "Get full details for one work (OpenAlex)", **_READONLY})
async def get_work_details(
    work_id: Annotated[str, Field(description="An OpenAlex work ID (e.g. 'W2741809807') or a DOI (e.g. '10.7717/peerj.4375').", min_length=3, max_length=200)],
    response_format: Annotated[ResponseFormat, Field(description="'markdown' for readable output or 'json' for structured data.")] = ResponseFormat.MARKDOWN,
) -> str:
    """Fetch the full record for a single work, including a reconstructed plain-text abstract.

    Accepts an OpenAlex work ID (e.g. 'W2741809807') or a DOI (e.g. '10.7717/peerj.4375').

    Returns (markdown): title, authors, year, venue, citations, DOI, open-access link and abstract.
    Returns (json): the summary fields plus "abstract" and "referenced_works_count".
    """
    if not _api_key():
        return _missing_key_message()

    raw = work_id.strip()
    if raw.lower().startswith("10.") or "doi.org" in raw.lower():
        doi = raw.split("doi.org/")[-1]
        path = f"/works/doi:{doi}"
    else:
        path = f"/works/{_short_id(raw)}"

    try:
        work = await _get(path, {"select": WORK_DETAIL_FIELDS})
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)

    abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
    summary = _work_summary_dict(work)

    if response_format is ResponseFormat.JSON:
        summary["abstract"] = abstract
        summary["referenced_works_count"] = work.get("referenced_works_count")
        return json.dumps(summary, indent=2, ensure_ascii=False)

    lines = [f"# {summary['title']} ({summary['year']})", f"**Authors:** {summary['authors']}"]
    if summary["venue"]:
        lines.append(f"**Venue:** {summary['venue']}")
    lines.append(f"**Cited by:** {summary['cited_by_count']}")
    if work.get("referenced_works_count") is not None:
        lines.append(f"**References:** {work.get('referenced_works_count')}")
    if summary["doi"]:
        lines.append(f"**DOI:** {summary['doi']}")
    if summary["open_access_url"]:
        lines.append(f"**Open access:** {summary['open_access_url']}")
    lines.append(f"**OpenAlex ID:** {summary['id']}")
    lines += ["", "## Abstract", abstract or "_No abstract available for this work._"]
    return "\n".join(lines)


@mcp.tool(name="find_citing_works", annotations={"title": "Find works that cite a given work (OpenAlex)", **_READONLY})
async def find_citing_works(
    work_id: Annotated[str, Field(description="The OpenAlex work ID to find citations of (e.g. 'W2741809807').", min_length=3, max_length=60)],
    max_results: Annotated[int, Field(description="How many citing works to return (1-100).", ge=1, le=100)] = 10,
    page: Annotated[int, Field(description="Results page, for paging beyond the first set.", ge=1)] = 1,
    sort: Annotated[SortOption, Field(description="Order by 'citations' (most cited first), 'date' (newest first) or 'relevance'.")] = SortOption.CITATIONS,
    response_format: Annotated[ResponseFormat, Field(description="'markdown' for a readable list or 'json' for structured data.")] = ResponseFormat.MARKDOWN,
) -> str:
    """Find the works that CITE a given work (forward citations) — how later scholarship has used it.

    This is the citation-graph workhorse for research: pass a work's OpenAlex ID to see who has cited
    it, newest or most-cited first. Get a work's ID from search_scholarship or get_work_details.

    Returns (markdown): a numbered list of citing works.
    Returns (json): {"total", "count", "page", "results":[{work summary}, ...]}
    """
    if not _api_key():
        return _missing_key_message()

    wid = _short_id(work_id.strip())
    api_params: dict[str, Any] = {"filter": f"cites:{wid}", "per_page": max_results, "page": page, "sort": SORT_MAP[sort]}
    return await _search_works(api_params, f"Works citing {wid}", page, response_format)


@mcp.tool(name="search_authors", annotations={"title": "Search for an author by name (OpenAlex)", **_READONLY})
async def search_authors(
    name: Annotated[str, Field(description="An author's name, e.g. 'Lawrence Lessig'.", min_length=1, max_length=200)],
    max_results: Annotated[int, Field(description="How many candidate authors to return (1-25).", ge=1, le=25)] = 10,
    response_format: Annotated[ResponseFormat, Field(description="'markdown' for a readable list or 'json' for structured data.")] = ResponseFormat.MARKDOWN,
) -> str:
    """Resolve an author name to OpenAlex author profiles, so you can then query their works by ID.

    Names are ambiguous, so OpenAlex's recommended pattern is to resolve a name to an author ID first,
    then call get_author_works with that ID. Returns each candidate's ID, institution, works count,
    citation count and h-index.

    Returns (markdown): a numbered list of candidate authors.
    Returns (json): {"total", "count", "results":[{"id","name","works_count","cited_by_count",
        "h_index","institution","orcid"}, ...]}
    """
    if not _api_key():
        return _missing_key_message()

    api_params = {"search": name, "per_page": max_results, "select": AUTHOR_FIELDS, "sort": "works_count:desc"}
    try:
        data = await _get("/authors", api_params)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)

    authors = data.get("results", [])
    total = (data.get("meta") or {}).get("count", len(authors))
    if response_format is ResponseFormat.JSON:
        payload = {"total": total, "count": len(authors), "results": [_author_summary_dict(a) for a in authors]}
        return json.dumps(payload, indent=2, ensure_ascii=False)
    return _format_author_summaries_md(authors, total)


@mcp.tool(name="get_author_works", annotations={"title": "Get an author's works by ID (OpenAlex)", **_READONLY})
async def get_author_works(
    author_id: Annotated[str, Field(description="An OpenAlex author ID (e.g. 'A5023888391') — get one from search_authors.", min_length=3, max_length=60)],
    max_results: Annotated[int, Field(description="How many works to return (1-100).", ge=1, le=100)] = 10,
    page: Annotated[int, Field(description="Results page, for paging beyond the first set.", ge=1)] = 1,
    sort: Annotated[SortOption, Field(description="Order by 'citations' (most cited first), 'date' (newest first) or 'relevance'.")] = SortOption.CITATIONS,
    response_format: Annotated[ResponseFormat, Field(description="'markdown' for a readable list or 'json' for structured data.")] = ResponseFormat.MARKDOWN,
) -> str:
    """List the works written by a given author, by their OpenAlex author ID.

    Use search_authors first to turn a name into an author ID, then pass it here. Useful for reviewing
    a scholar's body of work, most-cited first by default.

    Returns (markdown): a numbered list of the author's works.
    Returns (json): {"total", "count", "page", "results":[{work summary}, ...]}
    """
    if not _api_key():
        return _missing_key_message()

    aid = _short_id(author_id.strip())
    api_params: dict[str, Any] = {
        "filter": f"authorships.author.id:{aid}",
        "per_page": max_results, "page": page, "sort": SORT_MAP[sort],
    }
    return await _search_works(api_params, f"Works by author {aid}", page, response_format)


if __name__ == "__main__":
    # argparse gives a clean --help; the server runs over stdio for local MCP clients.
    argparse.ArgumentParser(
        description="Legal scholarship MCP server (OpenAlex) — runs over stdio for local MCP clients."
    ).parse_args()
    mcp.run()
