# legal\_scholarship\_mcp

An **MCP (Model Context Protocol) server** that lets an AI assistant search scholarly literature — useful for legal and academic research — through the [OpenAlex](https://openalex.org) database of 250M+ works. It exposes **five read-only tools** that a client such as Claude Desktop can call directly.

> \*\*Personal proof-of-concept, in active development.\*\* It returns scholarly \*\*metadata and abstracts\*\* — not full-text articles — and it does not give legal advice. The tools below work; more are planned (see Status).

## What it does

* **`search\_scholarship`** — full-text search of works by query, with year filters and sort by relevance / citations / date. Returns title, authors, year, venue, citation count, OpenAlex ID and DOI.
* **`get\_work\_details`** — fetch one work's full record by OpenAlex ID or DOI, including a plain-text abstract reconstructed from OpenAlex's inverted index, and its reference count.
* **`find\_citing\_works`** — the citation-graph workhorse: given a work's ID, find the works that **cite** it (forward citations), newest or most-cited first.
* **`search\_authors`** — resolve an author **name to OpenAlex author profiles** (ID, institution, works count, citations, h-index). Names are ambiguous, so this implements OpenAlex's recommended "resolve to an ID first" pattern.
* **`get\_author\_works`** — given an author **ID** (from `search\_authors`), list that author's works.

Every tool returns either readable **Markdown** or structured **JSON**, and all are read-only.

## What it does **not** do

* It does **not** return full-text articles (OpenAlex provides metadata and abstracts).
* It does **not** give legal advice or evaluate authority — it finds and summarises sources for a human to read.

## Requirements

```bash
pip install -r requirements.txt
```

### OpenAlex API key (free, required)

Since **13 February 2026, OpenAlex requires a free API key** for all requests (the old "polite pool" / `mailto` parameter was retired). Get one in about 30 seconds:

1. Create a free account at [https://openalex.org](https://openalex.org).
2. Copy your key from [https://openalex.org/settings/api](https://openalex.org/settings/api).
3. Set it in your environment:

```bash
export OPENALEX\_API\_KEY="your-free-key"
```

A free key gives **$1 of usage per day** — ample for interactive research. The key is sent as the `api\_key` query parameter on each request.

## Run it

**Inspect the tools with the MCP Inspector:**

```bash
mcp dev legal\_scholarship\_mcp.py
```

**Connect it to Claude Desktop** — add this to your `claude\_desktop\_config.json` (Settings → Developer → Edit Config), using the absolute path to the file:

```json
{
  "mcpServers": {
    "legal-scholarship": {
      "command": "python",
      "args": \["/absolute/path/to/legal\_scholarship\_mcp.py"],
      "env": { "OPENALEX\_API\_KEY": "your-free-key" }
    }
  }
}
```

Restart Claude Desktop, and the five tools become available in chat.

## How it works

Built on the MCP Python SDK's **FastMCP**. Each tool validates its inputs with Pydantic field constraints, calls OpenAlex asynchronously via `httpx`, and formats the result as Markdown or JSON. Shared logic — the API request (with a polite `User-Agent` and **retry-with-exponential-backoff** on transient 429/5xx/timeout failures, as OpenAlex recommends), error handling, abstract reconstruction, field formatting and the works-list path — is factored into helper functions. Author queries follow OpenAlex's "resolve a name to an ID, then filter by ID" pattern. Transport is stdio, which is what local clients like Claude Desktop use.

## Tests

```bash
pip install pytest
pytest
```

The suite runs **fully offline**: the deterministic helpers are tested directly, the tools are tested with the network call replaced by a fake, and the retry logic is tested with httpx's `MockTransport`. No API key or network access is needed. (One test is a regression guard ensuring the pagination parameter stays `per\_page`, OpenAlex's required spelling.)

## Status \& roadmap

**Working now:** the five read-only tools above, with input validation, retries, and Markdown/JSON output.

**Planned (this is an in-development prototype):**

* **Structured tool output** — define `outputSchema` / return `structuredContent` per MCP best practice, so clients get typed results rather than text/JSON strings.
* **More entity coverage** — tools for topics, institutions and sources; a related-works tool and a reference-list (backward-citation) tool.
* **Open-access PDF retrieval** for works that have a free full text.
* **An MCP evaluation suite** — a set of realistic, verifiable questions (per the MCP evaluation methodology) run against a live key.
* **Response caching** to reduce repeat calls and cost.

## Boundaries and responsible use

*Proof-of-concept in development, for demonstration and research only. Not legal advice; it returns scholarly metadata and abstracts, not full text, and does not evaluate the authority or correctness of any source. Confirm anything important against the original. Built through AI-assisted development.*

## Licence

MIT — see [LICENSE](LICENSE).

## About

Built by **Mahmoud Hussein**, an Egypt-qualified lawyer (registered at Appeal level), through AI-assisted development — directing and assembling AI-generated code on a foundation of Python, with the design and the OpenAlex API verification his own. This server is an actively-developed work in progress.

