# State Protocol

The helper stores durable local state in SQLite. The installed skill is
replaceable; the database is user data and must not be deleted when the skill
is updated.

## Input envelope

`record`, `check`, and `archive-stage` accept a JSON object through
`--input FILE` (or `-` for stdin):

```json
{
  "project_id": "stable-codex-project-id",
  "project_name": "Project name",
  "project_path": "/absolute/path",
  "zotero_library_id": 123,
  "recommendation_date": "2026-09-09",
  "queries": ["query used for the search"],
  "sources": ["Crossref", "PubMed"],
  "candidate_ids": ["doi:10.x/..."] ,
  "selection_notes": {"method": "directly useful"},
  "papers": [
    {
      "title": "Verified title",
      "authors": ["First Author"],
      "year": 2024,
      "venue": "Journal name",
      "doi": "https://doi.org/10.x/example",
      "pmid": "12345678",
      "arxiv_id": "2401.01234v2",
      "url": "https://doi.org/10.x/example",
      "abstract": "Author abstract",
      "summary": "Chinese summary",
      "relevance_reason": "Concrete connection to the project",
      "reading_focus": "Specific method or result to inspect",
      "evidence_basis": "abstract"
    }
  ]
}
```

`record` saves the final recommendation and its audit trail in one local
transaction. `check` is read-only with respect to recommendations and returns
one result per candidate, including `work_id`, normalized aliases,
`already_recommended`, `match_basis`, and `match_reason`. Do not read a large
history list into the model to perform exclusion manually.

The optional `identifiers` or `identifier_aliases` field can contain a mapping
such as `{"doi": "10.x/...", "arxiv": "2401.01234"}` or objects such as
the following. Mapping values are stored as unverified aliases; use an object
with `verified: true` (or `verified_aliases`) when the source confirms the
relationship:

```json
[{"type": "doi", "value": "10.x/...", "verified": true, "source": "publisher metadata"}]
```

Only aliases explicitly marked `verified` (and the primary DOI, PMID, or arXiv
fields supplied by the verified search result) are used to associate records.
An uncertain preprint/final relationship stays separate until metadata or a
source provides evidence. Supplying the same verified `work_id` is the most
direct way to record a confirmed relationship.

## Identity

The helper normalizes the following stable identifiers:

- DOI URLs, `doi:` prefixes, percent-encoded DOI paths, and citation terminal
  punctuation. Balanced legal suffixes such as `(supplement)` are retained.
- PMID numbers, `PMID:`/`PubMed:` forms, and common PubMed URLs.
- arXiv bare IDs, `arXiv:` forms, `/abs/` and `/pdf/` URLs, optional `.pdf`,
  and version suffixes. Versions map to the base arXiv work.

`work_id` is stable for the lifetime of a work. If the input gives one, it is
used. Otherwise a new work starts with `work:doi:...`, `work:pmid:...`, or
`work:arxiv:...`; records without a stable identifier use a deterministic
hash of normalized title, first author, year, and venue. `canonical_id` uses
the normalized stable identifier or the full `fallback:title:...|author:...|year:...|venue:...`
identity; `legacy_paper_id` remains available for old callers.

The project mapping also stores the Zotero personal-library ID when it is
returned by MCP, alongside the collection key and name.

When resolving a candidate, the database checks, in order:

1. Explicit `work_id` and verified identifier aliases.
2. An exact fallback key containing normalized title, first author, year, and
   venue.

Same title and year alone never merge two records. If aliases point to more
than one work, the helper returns an identity conflict and the skill records
`needs_input` rather than guessing.

## Durable records

- `works` is the identity table; `identifiers` is its DOI/PMID/arXiv alias
  table.
- `recommendations` is per project and date. It is independent of Zotero
  success, so a failed archive does not make a paper eligible again.
- `runs` stores queries, sources, candidate identifiers, exclusion count,
  selection notes, and final work IDs.
- `archive_operations` identifies one recommendation batch.
- `archive_stages` stores independent collection, item, membership, note, and
  verification progress.
- `archive_state` is the current per-project/work recovery view. `pending`
  includes every state except `saved`; it also returns the complete paper
  payload and recommendation reason.

Allowed archive states are exactly:
`pending`, `in_progress`, `partial`, `saved`, `failed`, `needs_input`.
Unknown values fail before the transaction commits.

`record` creates the recommendation, run, archive operation, pending archive
state, and initial `prepare` stage atomically. Its operation ID is deterministic
for project, date, and work set unless the input supplies `operation_id`; a
repeated batch therefore reuses the existing operation. `archive-stage` is
idempotent at `(operation_id, work_id, stage)` and increments a local attempt
counter without deleting the previous result.

## Project rename decisions

`project` compares the live Codex project name with the recorded Zotero
collection name. A mismatch returns `rename_required` and stores the pending
name. Run the command again with `--rename-decision keep` to keep the old
collection name, or `--rename-decision sync` after Zotero has been renamed.
The exact old-name/new-name pair is persisted, so the same change is not asked
again. `project-map` is read-only and exposes the mapping and past decisions.

## Migration and locking

On first opening an older database, the helper creates a timestamped SQLite
backup before adding the v4 tables and columns. It preserves legacy rows and
identifiers and does not merge ambiguous records. `migrate` reports the schema
version and latest backup path.

All local mutations use a file mutex and SQLite `BEGIN IMMEDIATE`. For a
multi-call Zotero workflow, acquire `archive-lock` first and pass its lease
token to every `archive-stage` call; release it with `archive-unlock`. An
expired lease is rejected, while local file locking still protects individual
commands.
