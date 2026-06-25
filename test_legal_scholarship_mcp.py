"""
Tests for legal_scholarship_mcp — run with: pytest

These run fully offline: the deterministic helpers are tested directly, the tools
are tested with the network call (`_get`) replaced by a fake, and the retry logic
is tested with httpx's MockTransport. No API key or network access is required.
"""

import asyncio
import json

import httpx
import pytest

import legal_scholarship_mcp as m

# --- canned OpenAlex responses ---------------------------------------------
WORKS = {
    "meta": {"count": 2},
    "results": [
        {
            "id": "https://openalex.org/W1", "display_name": "Paper One", "publication_year": 2020,
            "cited_by_count": 10, "doi": "https://doi.org/10.1/x",
            "authorships": [{"author": {"display_name": "A. Smith"}}],
            "primary_location": {"source": {"display_name": "Journal of Law"}},
            "open_access": {"oa_url": "https://oa/1"},
        },
        {
            "id": "https://openalex.org/W2", "display_name": "Paper Two", "publication_year": 2021,
            "cited_by_count": 5, "authorships": [], "primary_location": {}, "open_access": {},
        },
    ],
}

DETAIL = {
    "id": "https://openalex.org/W1", "display_name": "Paper One", "publication_year": 2020,
    "cited_by_count": 10, "doi": "https://doi.org/10.1/x",
    "authorships": [{"author": {"display_name": "A. Smith"}}],
    "primary_location": {"source": {"display_name": "Journal of Law"}},
    "open_access": {"oa_url": "https://oa/1"},
    "abstract_inverted_index": {"Hello": [0], "world": [1]}, "referenced_works_count": 12,
}

AUTHORS = {
    "meta": {"count": 1},
    "results": [{
        "id": "https://openalex.org/A1", "display_name": "Lawrence Lessig", "works_count": 100,
        "cited_by_count": 5000, "summary_stats": {"h_index": 40},
        "last_known_institutions": [{"display_name": "Harvard University"}],
        "orcid": "https://orcid.org/0000",
    }],
}


def fake_get_returning(canned, recorder):
    async def _fake(path, params, client=None):
        recorder["path"] = path
        recorder["params"] = params
        return canned
    return _fake


# --- pure helpers -----------------------------------------------------------
def test_reconstruct_abstract():
    assert m._reconstruct_abstract({"Hello": [0], "world": [1]}) == "Hello world"
    assert m._reconstruct_abstract(None) is None
    assert m._reconstruct_abstract({}) is None


def test_short_id():
    assert m._short_id("https://openalex.org/W123") == "W123"
    assert m._short_id("W123") == "W123"
    assert m._short_id("") == ""


def test_authors_truncates():
    work = {"authorships": [{"author": {"display_name": f"Author {i}"}} for i in range(8)]}
    out = m._authors(work, limit=3)
    assert out.startswith("Author 0, Author 1, Author 2")
    assert out.endswith("et al.")


def test_work_summary_dict():
    s = m._work_summary_dict(WORKS["results"][0])
    assert s["id"] == "W1"
    assert s["venue"] == "Journal of Law"
    assert s["open_access_url"] == "https://oa/1"


def test_author_summary_dict():
    s = m._author_summary_dict(AUTHORS["results"][0])
    assert s["id"] == "A1"
    assert s["h_index"] == 40
    assert s["institution"] == "Harvard University"


def test_handle_api_error_maps_status_codes():
    req = httpx.Request("GET", "https://api.openalex.org/works")
    for code, needle in [(403, "valid"), (404, "404"), (409, "credits"), (429, "rate limit")]:
        err = httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))
        assert needle in m._handle_api_error(err)
    assert "timed out" in m._handle_api_error(httpx.TimeoutException("t"))


# --- tools (with _get faked) ------------------------------------------------
def test_search_uses_snake_case_per_page(monkeypatch):
    """Regression guard: OpenAlex requires `per_page`, not the old `per-page`."""
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    rec = {}
    monkeypatch.setattr(m, "_get", fake_get_returning(WORKS, rec))
    asyncio.run(m.search_scholarship("ai copyright", from_year=2019, max_results=5))
    assert rec["params"]["per_page"] == 5
    assert "per-page" not in rec["params"]
    assert rec["params"]["search"] == "ai copyright"
    assert "from_publication_date:2019-01-01" in rec["params"]["filter"]


def test_search_json_output(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setattr(m, "_get", fake_get_returning(WORKS, {}))
    out = asyncio.run(m.search_scholarship("x", response_format=m.ResponseFormat.JSON))
    data = json.loads(out)
    assert data["total"] == 2 and data["count"] == 2
    assert data["results"][0]["id"] == "W1"


def test_find_citing_works_uses_cites_filter(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    rec = {}
    monkeypatch.setattr(m, "_get", fake_get_returning(WORKS, rec))
    out = asyncio.run(m.find_citing_works("https://openalex.org/W1"))
    assert rec["params"]["filter"] == "cites:W1"
    assert "Works citing W1" in out


def test_search_authors(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    rec = {}
    monkeypatch.setattr(m, "_get", fake_get_returning(AUTHORS, rec))
    out = asyncio.run(m.search_authors("Lawrence Lessig"))
    assert rec["path"] == "/authors"
    assert rec["params"]["search"] == "Lawrence Lessig"
    assert "Lawrence Lessig" in out and "h-index: 40" in out


def test_get_author_works_uses_author_filter(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    rec = {}
    monkeypatch.setattr(m, "_get", fake_get_returning(WORKS, rec))
    asyncio.run(m.get_author_works("A1"))
    assert rec["params"]["filter"] == "authorships.author.id:A1"


def test_get_work_details_routes_doi_vs_id(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    rec = {}
    monkeypatch.setattr(m, "_get", fake_get_returning(DETAIL, rec))
    asyncio.run(m.get_work_details("10.1/x"))
    assert rec["path"] == "/works/doi:10.1/x"
    asyncio.run(m.get_work_details("W1"))
    assert rec["path"] == "/works/W1"


def test_get_work_details_markdown_has_abstract(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setattr(m, "_get", fake_get_returning(DETAIL, {}))
    out = asyncio.run(m.get_work_details("W1"))
    assert "## Abstract" in out and "Hello world" in out
    assert "**References:** 12" in out


def test_missing_key_message(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    out = asyncio.run(m.search_scholarship("x"))
    assert "no OpenAlex API key" in out


def test_tool_surfaces_api_error(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    req = httpx.Request("GET", "https://api.openalex.org/works")

    async def boom(path, params, client=None):
        raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(404, request=req))

    monkeypatch.setattr(m, "_get", boom)
    out = asyncio.run(m.find_citing_works("W1"))
    assert "404" in out


# --- _get retry/backoff via MockTransport -----------------------------------
def test_get_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(m, "_BACKOFF_BASE", 0)  # no real waiting
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await m._get("/works", {"search": "x"}, client=client)

    assert asyncio.run(run()) == {"ok": True}
    assert calls["n"] == 3  # two 429s, then success
