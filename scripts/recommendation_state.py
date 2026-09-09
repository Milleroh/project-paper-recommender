#!/usr/bin/env python3
"""Persist paper recommendation history and Zotero archive progress.

The database is deliberately local and durable. It records recommendation
identity separately from archive identity so a Zotero outage never causes a
paper to be recommended again or an already-created item to be recreated.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import unquote, urlsplit


STATE_DIR_ENV = "CODEX_PROJECT_PAPER_RECOMMENDER_STATE_DIR"
DEFAULT_DB_NAME = "recommendations.sqlite3"
SCHEMA_VERSION = 4

ARCHIVE_STATUSES = frozenset(
    {"pending", "in_progress", "partial", "saved", "failed", "needs_input"}
)
ARCHIVE_STAGES = frozenset(
    {"prepare", "collection", "item", "membership", "note", "verify", "complete"}
)
STAGE_ALIASES = {
    "item_create": "item",
    "item-create": "item",
    "collection_create": "collection",
    "collection-create": "collection",
    "collection_add": "membership",
    "collection-add": "membership",
    "note_create": "note",
    "note-create": "note",
    "verification": "verify",
}
IDENTIFIER_PRIORITY = ("doi", "pmid", "arxiv")


class IdentityConflict(ValueError):
    """Raised when identifiers would merge two independently known works."""


def default_state_dir() -> Path:
    configured = os.environ.get(STATE_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Codex" / "project-paper-recommender"
    return Path.home() / ".local" / "state" / "codex" / "project-paper-recommender"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().date().isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int_or_zero(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _decode_identifier(value: Any) -> str:
    text = clean_text(value)
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text.strip()


def _trim_outer_quotes(text: str) -> str:
    quote_chars = "\"'\u201c\u201d\u2018\u2019"
    while len(text) >= 2 and text[0] in quote_chars and text[-1] in quote_chars:
        text = text[1:-1].strip()
    if len(text) >= 2 and text[0] == "<" and text[-1] == ">":
        text = text[1:-1].strip()
    return text


def _trim_citation_tail(text: str) -> str:
    """Remove punctuation outside an identifier's balanced wrappers."""

    text = text.strip()
    while text and text[-1] in ".,;:":
        text = text[:-1].rstrip()
    pairs = ((")", "("), ("]", "["), ("}", "{"), (">", "<"))
    changed = True
    while text and changed:
        changed = False
        for closing, opening in pairs:
            if text.endswith(closing) and text.count(closing) > text.count(opening):
                text = text[:-1].rstrip()
                changed = True
                break
    return text


def normalize_doi(value: Any) -> str:
    text = _trim_outer_quotes(_decode_identifier(value))
    text = re.sub(
        r"^(?:https?://)?(?:www\.)?(?:dx\.)?doi\.org/",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^doi\s*:\s*", "", text, flags=re.IGNORECASE)
    text = _trim_citation_tail(text).casefold()
    if not re.fullmatch(r"10\.\d{4,9}/\S+", text):
        return ""
    return text


def normalize_pmid(value: Any) -> str:
    text = _trim_outer_quotes(_decode_identifier(value))
    if re.fullmatch(r"\d+", text):
        return text

    parsed = urlsplit(text if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I) else "")
    host = parsed.netloc.casefold()
    if host and ("pubmed" in host or host.endswith("ncbi.nlm.nih.gov")):
        path_match = re.search(r"/(?:pubmed/)?(\d+)(?:/|$)", parsed.path, re.I)
        if path_match:
            return path_match.group(1)
    prefix = re.match(r"^(?:pmid|pubmed)\s*[:#]?\s*(\d+)\b", text, re.I)
    return prefix.group(1) if prefix else ""


def normalize_arxiv(value: Any) -> str:
    text = _trim_outer_quotes(_decode_identifier(value))
    parsed = urlsplit(text if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I) else "")
    if parsed.netloc and "arxiv.org" in parsed.netloc.casefold():
        path = parsed.path.strip("/")
        match = re.match(r"(?:abs|pdf)/(.+)", path, re.I)
        text = match.group(1) if match else path
        text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    else:
        text = re.sub(r"^arxiv\s*:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^(?:abs|pdf)/", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    text = _trim_citation_tail(text).casefold()
    text = re.sub(r"v\d+$", "", text)
    new_id = r"\d{4}\.\d{4,5}"
    old_id = r"[a-z][a-z0-9-]*(?:\.[a-z]{2})?/\d{7}"
    if not re.fullmatch(rf"(?:{new_id}|{old_id})", text, flags=re.IGNORECASE):
        return ""
    return text


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    text = "".join(char if char.isalnum() else " " for char in text)
    return " ".join(text.split())


def first_author(paper: dict[str, Any]) -> str:
    authors = paper.get("authors") or paper.get("creators") or []
    if isinstance(authors, dict):
        authors = [authors]
    if not isinstance(authors, list) or not authors:
        return ""
    author = authors[0]
    if isinstance(author, dict):
        return clean_text(
            author.get("lastName")
            or author.get("family")
            or author.get("name")
            or author.get("firstName")
        )
    return clean_text(author)


def paper_year(paper: dict[str, Any]) -> str:
    value = paper.get("year")
    if value is None:
        value = paper.get("date") or paper.get("issued")
    match = re.search(r"(?:18|19|20|21)\d{2}", clean_text(value))
    return match.group(0) if match else clean_text(value)


def paper_venue(paper: dict[str, Any]) -> str:
    return clean_text(
        paper.get("venue")
        or paper.get("journal")
        or paper.get("publicationTitle")
        or paper.get("journal_title")
        or paper.get("container_title")
    )


def legacy_paper_id(paper: dict[str, Any]) -> str:
    """Return the pre-v4 canonical ID for compatibility with old callers."""

    doi = normalize_doi(paper.get("doi"))
    if doi:
        return f"doi:{doi}"
    pmid = normalize_pmid(paper.get("pmid"))
    if pmid:
        return f"pmid:{pmid}"
    arxiv = normalize_arxiv(paper.get("arxiv_id") or paper.get("arxiv"))
    if arxiv:
        return f"arxiv:{arxiv}"
    title = normalize_title(paper.get("title"))
    if not title:
        raise ValueError("paper.title is required when no stable identifier is available")
    return f"title:{title}|year:{paper_year(paper)}"


def paper_id(paper: dict[str, Any]) -> str:
    """Return the v4 identity ID, including strong fallback metadata."""

    alias = primary_alias(paper)
    if alias:
        return f"{alias['type']}:{alias['value']}"
    return "fallback:" + fallback_key(paper)


def fallback_key(paper: dict[str, Any]) -> str:
    title = normalize_title(paper.get("title"))
    if not title:
        raise ValueError("paper.title is required")
    author = normalize_title(first_author(paper))
    venue = normalize_title(paper_venue(paper))
    return f"title:{title}|author:{author}|year:{paper_year(paper)}|venue:{venue}"


def canonical_id_for(paper: dict[str, Any]) -> str:
    """Return the durable canonical ID used by the v4 recommendation table."""

    alias = primary_alias(paper)
    if alias:
        return f"{alias['type']}:{alias['value']}"
    return "fallback:" + fallback_key(paper)


def _normalize_alias(alias_type: Any, value: Any) -> str:
    kind = clean_text(alias_type).casefold().replace("arxiv_id", "arxiv")
    if kind == "doi":
        return normalize_doi(value)
    if kind == "pmid":
        return normalize_pmid(value)
    if kind == "arxiv":
        return normalize_arxiv(value)
    return ""


def _add_alias(
    aliases: dict[tuple[str, str], dict[str, Any]],
    alias_type: Any,
    value: Any,
    *,
    verified: bool,
    source: str,
) -> None:
    kind = clean_text(alias_type).casefold().replace("arxiv_id", "arxiv")
    normalized = _normalize_alias(kind, value)
    if kind not in IDENTIFIER_PRIORITY or not normalized:
        return
    key = (kind, normalized)
    old = aliases.get(key)
    if old is None or (verified and not old["verified"]):
        aliases[key] = {
            "type": kind,
            "value": normalized,
            "verified": bool(verified),
            "source": clean_text(source),
        }


def identifier_aliases(paper: dict[str, Any]) -> list[dict[str, Any]]:
    aliases: dict[tuple[str, str], dict[str, Any]] = {}
    _add_alias(aliases, "doi", paper.get("doi"), verified=True, source="doi")
    _add_alias(aliases, "pmid", paper.get("pmid"), verified=True, source="pmid")
    _add_alias(
        aliases,
        "arxiv",
        paper.get("arxiv_id") or paper.get("arxiv"),
        verified=True,
        source="arxiv",
    )

    for key in ("verified_aliases", "verified_identifiers"):
        raw = paper.get(key)
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, dict):
                    _add_alias(
                        aliases,
                        item.get("type") or item.get("id_type"),
                        item.get("value") or item.get("identifier") or item.get("id"),
                        verified=True,
                        source=item.get("source") or key,
                    )
                elif isinstance(item, str) and ":" in item:
                    kind, value = item.split(":", 1)
                    _add_alias(aliases, kind, value, verified=True, source=key)

    raw_identifiers = paper.get("identifiers") or paper.get("identifier_aliases")
    if isinstance(raw_identifiers, dict):
        if any(key in raw_identifiers for key in ("type", "id_type", "value", "identifier", "id")):
            raw_identifiers = [raw_identifiers]
        else:
            expanded: list[dict[str, Any]] = []
            for kind, values in raw_identifiers.items():
                values = values if isinstance(values, list) else [values]
                expanded.extend({"type": kind, "value": item} for item in values)
            raw_identifiers = expanded
    if isinstance(raw_identifiers, list):
        for item in raw_identifiers:
            if isinstance(item, dict):
                _add_alias(
                    aliases,
                    item.get("type") or item.get("id_type"),
                    item.get("value") or item.get("identifier") or item.get("id"),
                    verified=bool(item.get("verified", item.get("confirmed", False))),
                    source=item.get("source") or "identifiers",
                )
            elif isinstance(item, str) and ":" in item:
                kind, value = item.split(":", 1)
                _add_alias(aliases, kind, value, verified=False, source="identifiers")

    raw_aliases = paper.get("aliases")
    if isinstance(raw_aliases, list):
        for item in raw_aliases:
            if isinstance(item, dict):
                _add_alias(
                    aliases,
                    item.get("type") or item.get("id_type"),
                    item.get("value") or item.get("identifier") or item.get("id"),
                    verified=bool(item.get("verified", False)),
                    source=item.get("source") or "aliases",
                )
            elif isinstance(item, str) and ":" in item:
                kind, value = item.split(":", 1)
                _add_alias(aliases, kind, value, verified=False, source="aliases")
    return [aliases[key] for key in sorted(aliases)]


def primary_alias(paper: dict[str, Any]) -> dict[str, Any] | None:
    aliases = identifier_aliases(paper)
    for kind in IDENTIFIER_PRIORITY:
        for alias in aliases:
            if alias["type"] == kind:
                return alias
    return None


def work_id_for(paper: dict[str, Any]) -> str:
    explicit = clean_text(paper.get("work_id"))
    if explicit:
        return explicit
    alias = primary_alias(paper)
    if alias:
        return f"work:{alias['type']}:{alias['value']}"
    digest = hashlib.sha256(fallback_key(paper).encode("utf-8")).hexdigest()[:24]
    return f"work:fallback:{digest}"


def paper_payload(paper: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(paper, ensure_ascii=False, sort_keys=True, default=str))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _from_json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(clean_text(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


@contextlib.contextmanager
def file_mutex(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Serialize local state mutations, including archive retries."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            import fcntl  # type: ignore

            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for state lock: {path}")
                    time.sleep(0.05)
        except ImportError:  # pragma: no cover - macOS/Linux use fcntl
            pass
        yield
    finally:
        try:
            import fcntl  # type: ignore

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _state_lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".lock")


@contextlib.contextmanager
def locked_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(db_path)
    try:
        with file_mutex(_state_lock_path(db_path)):
            yield connection
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _has_user_tables(connection: sqlite3.Connection) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
    )


def _backup_before_migration(connection: sqlite3.Connection, db_path: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    suffix = 1
    while backup.exists():
        backup = db_path.with_name(f"{db_path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    backup_connection = sqlite3.connect(str(backup))
    try:
        connection.backup(backup_connection)
    finally:
        backup_connection.close()
    return str(backup)


def _safe_payload(value: Any) -> dict[str, Any]:
    parsed = _from_json(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _aliases_from_legacy(canonical_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    aliases = identifier_aliases(payload)
    if aliases:
        return aliases
    match = re.match(r"^(doi|pmid|arxiv):(.+)$", clean_text(canonical_id), re.I)
    if not match:
        return []
    normalized = _normalize_alias(match.group(1), match.group(2))
    return (
        [{"type": match.group(1).casefold(), "value": normalized, "verified": True, "source": "legacy"}]
        if normalized
        else []
    )


def _insert_work(
    connection: sqlite3.Connection,
    work_id: str,
    paper: dict[str, Any],
    *,
    created_at: str | None = None,
) -> None:
    timestamp = created_at or now_iso()
    title = clean_text(paper.get("title"))
    connection.execute(
        """
        INSERT INTO works(work_id, title, first_author, year, venue, fallback_key,
                          metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE works.title END,
            first_author = CASE WHEN excluded.first_author != '' THEN excluded.first_author
                                ELSE works.first_author END,
            year = CASE WHEN excluded.year != '' THEN excluded.year ELSE works.year END,
            venue = CASE WHEN excluded.venue != '' THEN excluded.venue ELSE works.venue END,
            fallback_key = CASE WHEN excluded.fallback_key != '' THEN excluded.fallback_key
                                ELSE works.fallback_key END,
            metadata_json = CASE WHEN excluded.metadata_json != '{}' THEN excluded.metadata_json
                                 ELSE works.metadata_json END,
            updated_at = excluded.updated_at
        """,
        (
            work_id,
            title,
            first_author(paper),
            paper_year(paper),
            paper_venue(paper),
            fallback_key(paper) if title else "",
            _json(paper_payload(paper)),
            timestamp,
            timestamp,
        ),
    )


def _find_alias_work_ids(
    connection: sqlite3.Connection, aliases: list[dict[str, Any]]
) -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for alias in aliases:
        if not bool(alias.get("verified")):
            continue
        rows = connection.execute(
            "SELECT work_id FROM identifiers WHERE identifier_type=? AND identifier_value=? AND verified=1",
            (alias["type"], alias["value"]),
        ).fetchall()
        if rows:
            found[(alias["type"], alias["value"])] = {row[0] for row in rows}
    return found


def _resolve_work_id(
    connection: sqlite3.Connection, paper: dict[str, Any], *, create: bool = False
) -> tuple[str, str]:
    """Return work ID and match basis, raising on an identity conflict."""

    aliases = identifier_aliases(paper)
    alias_matches = _find_alias_work_ids(connection, aliases)
    matched = set().union(*alias_matches.values()) if alias_matches else set()
    explicit = clean_text(paper.get("work_id"))
    if explicit:
        if matched and matched != {explicit}:
            raise IdentityConflict(
                f"work_id {explicit!r} conflicts with identifier work(s): {sorted(matched)}"
            )
        chosen = explicit
        basis = "work_id" if not matched else "identifier_alias"
    elif len(matched) > 1:
        details = ", ".join(
            f"{kind}:{value} -> {sorted(ids)}"
            for (kind, value), ids in alias_matches.items()
        )
        raise IdentityConflict(f"identifiers resolve to different works: {details}")
    elif matched:
        chosen = next(iter(matched))
        basis = next(
            (kind for (kind, _), ids in alias_matches.items() if chosen in ids),
            "identifier_alias",
        )
    else:
        candidate_fallback = fallback_key(paper)
        row = connection.execute(
            "SELECT work_id FROM works WHERE fallback_key=? ORDER BY work_id LIMIT 2",
            (candidate_fallback,),
        ).fetchall()
        if len(row) > 1:
            raise IdentityConflict(f"fallback identity is ambiguous: {candidate_fallback}")
        if row:
            chosen = row[0][0]
            basis = "title_author_year_venue"
        else:
            chosen = work_id_for(paper)
            basis = "new_identifier" if aliases else "new_fallback"

    if create:
        _insert_work(connection, chosen, paper)
        for alias in aliases:
            existing = connection.execute(
                "SELECT work_id FROM identifiers WHERE identifier_type=? AND identifier_value=?",
                (alias["type"], alias["value"]),
            ).fetchone()
            if existing and existing[0] != chosen:
                raise IdentityConflict(
                    f"identifier {alias['type']}:{alias['value']} belongs to {existing[0]}"
                )
            connection.execute(
                """
                INSERT INTO identifiers(identifier_type, identifier_value, work_id,
                                        verified, source, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                    verified = MAX(identifiers.verified, excluded.verified),
                    source = CASE WHEN excluded.source != '' THEN excluded.source
                                  ELSE identifiers.source END,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    alias["type"],
                    alias["value"],
                    chosen,
                    int(bool(alias["verified"])),
                    alias.get("source", ""),
                    now_iso(),
                    now_iso(),
                ),
            )
    return chosen, basis


def _project_values(data: dict[str, Any]) -> tuple[str, str, str]:
    project_id = clean_text(data.get("project_id"))
    project_name = clean_text(data.get("project_name"))
    project_path = clean_text(data.get("project_path"))
    if not project_id or not project_name:
        raise ValueError("project_id and project_name are required")
    return project_id, project_name, project_path


def _project_row(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()


def _rename_decision_row(
    connection: sqlite3.Connection, project_id: str, old_name: str, new_name: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM project_rename_decisions
        WHERE project_id=? AND old_name=? AND new_name=?
        """,
        (project_id, old_name, new_name),
    ).fetchone()


def upsert_project(
    connection: sqlite3.Connection,
    data: dict[str, Any],
    *,
    rename_decision: str = "",
    collection_key: str = "",
    collection_name: str = "",
    library_id: Any = 0,
) -> dict[str, Any]:
    """Upsert a project and return an explicit rename prompt when needed."""

    project_id, project_name, project_path = _project_values(data)
    existing = _project_row(connection, project_id)
    timestamp = now_iso()
    zotero_library_id = _int_or_zero(library_id)
    if existing is None:
        connection.execute(
            """
            INSERT INTO projects(project_id, project_name, project_path,
                                 collection_key, collection_name, pending_project_name,
                                 rename_pending, zotero_library_id, updated_at)
            VALUES (?, ?, ?, ?, ?, '', 0, ?, ?)
            """,
            (
                project_id,
                project_name,
                project_path,
                clean_text(collection_key),
                clean_text(collection_name),
                zotero_library_id,
                timestamp,
            ),
        )
        row = _project_row(connection, project_id)
        result = {"project": dict(row), "rename_required": False}
        result.update(dict(row))
        return result

    old_name = clean_text(existing["project_name"])
    remote_collection_name = clean_text(existing["collection_name"])
    decision = _rename_decision_row(connection, project_id, old_name, project_name)
    requested = clean_text(rename_decision).casefold()
    rename_required = False
    rename_info: dict[str, Any] | None = None

    if old_name != project_name and remote_collection_name and remote_collection_name != project_name:
        if decision is None and requested not in {"keep", "sync"}:
            connection.execute(
                """
                UPDATE projects
                SET pending_project_name=?, rename_pending=1,
                    project_path=CASE WHEN ? != '' THEN ? ELSE project_path END,
                    updated_at=?
                WHERE project_id=?
                """,
                (project_name, project_path, project_path, timestamp, project_id),
            )
            rename_required = True
            rename_info = {
                "project_id": project_id,
                "old_name": old_name,
                "new_name": project_name,
                "collection_key": clean_text(existing["collection_key"]),
                "collection_name": remote_collection_name,
                "choices": ["keep", "sync"],
            }
        else:
            choice = requested or clean_text(decision["decision"])
            if choice not in {"keep", "sync"}:
                raise ValueError("rename decision must be keep or sync")
            final_collection_name = project_name if choice == "sync" else remote_collection_name
            final_collection_key = clean_text(collection_key) or clean_text(existing["collection_key"])
            connection.execute(
                """
                INSERT INTO project_rename_decisions(project_id, old_name, new_name,
                                                     decision, decided_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, old_name, new_name) DO UPDATE SET
                    decision=excluded.decision, decided_at=excluded.decided_at
                """,
                (project_id, old_name, project_name, choice, timestamp),
            )
            connection.execute(
                """
                UPDATE projects
                SET project_name=?, project_path=CASE WHEN ? != '' THEN ? ELSE project_path END,
                    collection_key=?, collection_name=?,
                    zotero_library_id=CASE WHEN ? > 0 THEN ? ELSE zotero_library_id END,
                    pending_project_name='',
                    rename_pending=0, updated_at=?
                WHERE project_id=?
                """,
                (
                    project_name,
                    project_path,
                    project_path,
                    final_collection_key,
                    final_collection_name,
                    zotero_library_id,
                    zotero_library_id,
                    timestamp,
                    project_id,
                ),
            )
            rename_info = {"old_name": old_name, "new_name": project_name, "decision": choice}
    else:
        connection.execute(
            """
            UPDATE projects
            SET project_name=?, project_path=CASE WHEN ? != '' THEN ? ELSE project_path END,
                collection_key=CASE WHEN ? != '' THEN ? ELSE collection_key END,
                collection_name=CASE WHEN ? != '' THEN ? ELSE collection_name END,
                zotero_library_id=CASE WHEN ? > 0 THEN ? ELSE zotero_library_id END,
                pending_project_name='', rename_pending=0, updated_at=?
            WHERE project_id=?
            """,
            (
                project_name,
                project_path,
                project_path,
                clean_text(collection_key),
                clean_text(collection_key),
                clean_text(collection_name),
                clean_text(collection_name),
                zotero_library_id,
                zotero_library_id,
                timestamp,
                project_id,
            ),
        )

    row = _project_row(connection, project_id)
    result: dict[str, Any] = {"project": dict(row), "rename_required": rename_required}
    if rename_info:
        result["rename"] = rename_info
    if row:
        result.update(dict(row))
    return result


def _ensure_project_ready(result: dict[str, Any]) -> None:
    if result.get("rename_required"):
        rename = result.get("rename") or {}
        raise ValueError(
            "project rename requires a decision: "
            f"{rename.get('old_name', '')!r} -> {rename.get('new_name', '')!r}; use --rename-decision keep|sync"
        )


def _record_run(
    connection: sqlite3.Connection,
    data: dict[str, Any],
    *,
    run_id: str,
    final_work_ids: list[str],
    excluded_count: int = 0,
) -> bool:
    queries = data.get("queries") if isinstance(data.get("queries"), list) else []
    sources = data.get("sources") if isinstance(data.get("sources"), list) else []
    candidate_ids = data.get("candidate_ids")
    if not isinstance(candidate_ids, list):
        candidate_ids = []
        candidates = data.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    candidate_ids.append(clean_text(candidate.get("work_id")) or canonical_id_for(candidate))
                else:
                    candidate_ids.append(clean_text(candidate))
    selection_notes = data.get("selection_notes", {})
    run_date = clean_text(data.get("recommendation_date")) or today()
    result = connection.execute(
        """
        INSERT OR IGNORE INTO runs(
            run_id, project_id, run_date, queries_json, sources_json, candidate_ids_json,
            selection_notes_json, candidate_count, excluded_count, final_work_ids_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            clean_text(data.get("project_id")),
            run_date,
            _json(queries),
            _json(sources),
            _json(candidate_ids),
            _json(selection_notes),
            len(candidate_ids),
            excluded_count,
            _json(final_work_ids),
            now_iso(),
        ),
    )
    return bool(result.rowcount)


def _make_run_id(project_id: str, date: str, data: dict[str, Any], work_ids: list[str]) -> str:
    explicit = clean_text(data.get("run_id"))
    if explicit:
        return explicit
    material = "|".join(
        [project_id, date, _json(data.get("queries", [])), _json(data.get("sources", [])), _json(sorted(work_ids))]
    )
    return "run-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _make_operation_id(project_id: str, date: str, data: dict[str, Any], work_ids: list[str]) -> str:
    explicit = clean_text(data.get("operation_id"))
    if explicit:
        return explicit
    material = "|".join([project_id, date, _json(sorted(work_ids))])
    return "op-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _recommendation_rows_for_work(
    connection: sqlite3.Connection, project_id: str, work_id: str
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM recommendations
        WHERE project_id=? AND (work_id=? OR (work_id='' AND canonical_id=?))
        ORDER BY recommended_at DESC
        """,
        (project_id, work_id, work_id),
    ).fetchall()


def _candidate_check(
    connection: sqlite3.Connection,
    project_id: str,
    paper: dict[str, Any],
    seen_work_ids: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(paper, dict):
        raise ValueError("each item in papers must be an object")
    title = clean_text(paper.get("title"))
    if not title:
        raise ValueError("paper.title is required")
    aliases = identifier_aliases(paper)
    try:
        work_id, basis = _resolve_work_id(connection, paper, create=False)
        conflict = ""
    except IdentityConflict as exc:
        work_id = clean_text(paper.get("work_id")) or work_id_for(paper)
        basis = "identity_conflict"
        conflict = str(exc)

    rows = _recommendation_rows_for_work(connection, project_id, work_id)
    if not rows and aliases:
        matched = _find_alias_work_ids(connection, aliases)
        for matched_work_ids in matched.values():
            for matched_work_id in matched_work_ids:
                rows.extend(_recommendation_rows_for_work(connection, project_id, matched_work_id))
    if not rows and basis == "new_fallback":
        fallback = fallback_key(paper)
        rows = connection.execute(
            """
            SELECT r.* FROM recommendations r
            JOIN works w ON w.work_id=r.work_id
            WHERE r.project_id=? AND w.fallback_key=?
            ORDER BY r.recommended_at DESC
            """,
            (project_id, fallback),
        ).fetchall()
        if rows:
            work_id = clean_text(rows[0]["work_id"])
            basis = "title_author_year_venue"

    duplicate_in_batch = bool(seen_work_ids is not None and work_id in seen_work_ids)
    already = bool(rows) or duplicate_in_batch
    matched_dates = [clean_text(row["recommended_on"]) for row in rows]
    if conflict:
        reason = conflict
    elif duplicate_in_batch:
        reason = "duplicate_in_batch"
    elif rows:
        identifiers = ", ".join(
            f"{alias['type']}:{alias['value']}" for alias in aliases if alias["type"] == basis
        )
        reason = f"{basis} match"
        if identifiers:
            reason += f" ({identifiers})"
    else:
        reason = "no project recommendation match"
    return {
        "work_id": work_id,
        "canonical_id": canonical_id_for(paper),
        "identifiers": aliases,
        "already_recommended": already,
        "duplicate_in_batch": duplicate_in_batch,
        "match_basis": basis,
        "match_reason": reason,
        "recommended_dates": sorted(set(matched_dates)),
        "needs_input": bool(conflict),
    }


def _validate_status(value: Any) -> str:
    status = clean_text(value).casefold()
    if status not in ARCHIVE_STATUSES:
        allowed = ", ".join(sorted(ARCHIVE_STATUSES))
        raise ValueError(f"invalid archive status {status!r}; allowed: {allowed}")
    return status


def _normalize_stage(value: Any) -> str:
    stage = clean_text(value).casefold().replace(" ", "_")
    stage = STAGE_ALIASES.get(stage, stage)
    if stage not in ARCHIVE_STAGES:
        allowed = ", ".join(sorted(ARCHIVE_STAGES))
        raise ValueError(f"invalid archive stage {stage!r}; allowed: {allowed}")
    return stage


def _paper_for_work(connection: sqlite3.Connection, project_id: str, work_id: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT payload_json FROM recommendations
        WHERE project_id=? AND (work_id=? OR (work_id='' AND canonical_id=?))
        ORDER BY recommended_at DESC LIMIT 1
        """,
        (project_id, work_id, work_id),
    ).fetchone()
    return _safe_payload(row[0]) if row else {}


def _canonical_for_work(
    connection: sqlite3.Connection, project_id: str, work_id: str, fallback: str = ""
) -> str:
    row = connection.execute(
        """
        SELECT canonical_id FROM recommendations
        WHERE project_id=? AND (work_id=? OR (work_id='' AND canonical_id=?))
        ORDER BY recommended_at DESC LIMIT 1
        """,
        (project_id, work_id, work_id),
    ).fetchone()
    return clean_text(row[0]) if row else fallback


def _resolve_archive_work_id(
    connection: sqlite3.Connection, project_id: str, result: dict[str, Any]
) -> tuple[str, str]:
    explicit = clean_text(result.get("work_id"))
    if explicit:
        if _recommendation_rows_for_work(connection, project_id, explicit):
            return explicit, "work_id"
        state_row = connection.execute(
            "SELECT work_id FROM archive_state WHERE project_id=? AND work_id=?", (project_id, explicit)
        ).fetchone()
        if state_row:
            return explicit, "work_id"

    candidate = dict(result)
    canonical = clean_text(result.get("canonical_id"))
    if canonical:
        direct_rows = connection.execute(
            """
            SELECT work_id FROM recommendations
            WHERE project_id=? AND canonical_id=?
            ORDER BY recommended_at DESC LIMIT 1
            """,
            (project_id, canonical),
        ).fetchall()
        if len(direct_rows) > 1:
            raise IdentityConflict(f"canonical_id matches multiple recommendations: {canonical}")
        if direct_rows and clean_text(direct_rows[0][0]):
            return clean_text(direct_rows[0][0]), "canonical_id"
    if canonical:
        match = re.match(r"^(doi|pmid|arxiv):(.+)$", canonical, re.I)
        if match:
            candidate[match.group(1).casefold()] = match.group(2)
        elif not candidate.get("title"):
            candidate["title"] = canonical
    aliases = identifier_aliases(candidate)
    matched: set[str] = set()
    for alias_ids in _find_alias_work_ids(connection, aliases).values():
        matched.update(alias_ids)
    project_matches = {
        clean_text(row["work_id"])
        for work_id in matched
        for row in _recommendation_rows_for_work(connection, project_id, work_id)
    }
    if len(project_matches) > 1:
        raise IdentityConflict(f"archive result matches multiple project works: {sorted(project_matches)}")
    if project_matches:
        return next(iter(project_matches)), "identifier"
    if candidate.get("title"):
        fallback = fallback_key(candidate)
        row = connection.execute(
            """
            SELECT r.work_id FROM recommendations r
            JOIN works w ON w.work_id=r.work_id
            WHERE r.project_id=? AND w.fallback_key=?
            ORDER BY r.recommended_at DESC LIMIT 2
            """,
            (project_id, fallback),
        ).fetchall()
        if len(row) == 1:
            return clean_text(row[0][0]), "title_author_year_venue"
    raise ValueError("archive result does not identify a recommendation; include work_id or stable identifier")


def _upsert_archive_compat(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    work_id: str,
    canonical_id: str,
    status: str,
    collection_key: str,
    collection_name: str,
    item_key: str,
    note_key: str,
    error: str,
    attempt_count: int,
    operation_id: str,
    stage: str,
    item_action: str,
    result_json: str,
) -> None:
    canonical_id = canonical_id or work_id
    connection.execute(
        """
        INSERT INTO archive_status(
            project_id, canonical_id, work_id, operation_id, stage, item_action,
            status, collection_key, collection_name, zotero_item_key, zotero_note_key,
            last_error, attempt_count, result_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, canonical_id) DO UPDATE SET
            work_id=CASE WHEN excluded.work_id != '' THEN excluded.work_id ELSE archive_status.work_id END,
            operation_id=CASE WHEN excluded.operation_id != '' THEN excluded.operation_id ELSE archive_status.operation_id END,
            stage=excluded.stage,
            item_action=CASE WHEN excluded.item_action != '' THEN excluded.item_action ELSE archive_status.item_action END,
            status=excluded.status,
            collection_key=CASE WHEN excluded.collection_key != '' THEN excluded.collection_key ELSE archive_status.collection_key END,
            collection_name=CASE WHEN excluded.collection_name != '' THEN excluded.collection_name ELSE archive_status.collection_name END,
            zotero_item_key=CASE WHEN excluded.zotero_item_key != '' THEN excluded.zotero_item_key ELSE archive_status.zotero_item_key END,
            zotero_note_key=CASE WHEN excluded.zotero_note_key != '' THEN excluded.zotero_note_key ELSE archive_status.zotero_note_key END,
            last_error=excluded.last_error,
            attempt_count=excluded.attempt_count,
            result_json=CASE WHEN excluded.result_json != '' THEN excluded.result_json ELSE archive_status.result_json END,
            updated_at=excluded.updated_at
        """,
        (
            project_id,
            canonical_id,
            work_id,
            operation_id,
            stage,
            item_action,
            status,
            collection_key,
            collection_name,
            item_key,
            note_key,
            error,
            attempt_count,
            result_json,
            now_iso(),
        ),
    )


def _archive_state_status(existing: str, incoming: str, stage: str) -> str:
    if stage not in {"complete", "verify"} and incoming == "saved":
        incoming = "partial"
    if existing == "saved" and incoming != "saved":
        return "saved"
    if incoming == "pending" and existing in {"in_progress", "partial", "saved"}:
        return existing
    return incoming


def _aggregate_operation_status(connection: sqlite3.Connection, operation_id: str) -> str:
    statuses = [clean_text(row[0]) for row in connection.execute("SELECT status FROM archive_state WHERE operation_id=?", (operation_id,)).fetchall()]
    if not statuses:
        return "pending"
    if all(status == "saved" for status in statuses):
        return "saved"
    for status in ("needs_input", "failed", "in_progress", "partial", "pending"):
        if status in statuses:
            return status
    return "pending"


def _check_archive_lease(connection: sqlite3.Connection, token: str, operation_id: str) -> None:
    row = connection.execute("SELECT token, operation_id, expires_at FROM archive_mutex WHERE mutex_id=1").fetchone()
    if not token:
        if row and clean_text(row[2]) > now_iso():
            raise ValueError("archive lock is held; pass its lock token to archive-stage")
        return
    if not row or row[0] != token or (row[1] and row[1] != operation_id):
        raise ValueError("archive lock token is missing, expired, or belongs to another operation")
    if clean_text(row[2]) <= now_iso():
        raise ValueError("archive lock token has expired")
    connection.execute(
        "UPDATE archive_mutex SET expires_at=? WHERE mutex_id=1",
        (datetime.fromtimestamp(time.time() + 300, timezone.utc).isoformat(timespec="seconds"),),
    )


def _apply_archive_results(
    connection: sqlite3.Connection,
    data: dict[str, Any],
    *,
    default_stage: str = "complete",
    require_operation: bool = True,
    lock_token: str = "",
) -> list[dict[str, Any]]:
    project_id, _, _ = _project_values(data)
    archive = data.get("archive") if isinstance(data.get("archive"), dict) else {}
    collection = archive.get("collection") if isinstance(archive.get("collection"), dict) else {}
    collection_key = clean_text(collection.get("key") or collection.get("collection_key"))
    collection_name = clean_text(collection.get("name") or collection.get("collection_name"))
    library_id = _int_or_zero(
        collection.get("libraryID")
        or collection.get("library_id")
        or data.get("zotero_library_id")
        or data.get("library_id")
    )
    raw_results = archive.get("papers") if isinstance(archive.get("papers"), list) else data.get("papers", [])
    if not isinstance(raw_results, list):
        raise ValueError("archive.papers must be a JSON list")
    operation_id = clean_text(data.get("operation_id"))
    if not operation_id and not require_operation:
        operation_id = "op-legacy-" + hashlib.sha256(
            (project_id + "|" + clean_text(data.get("recommendation_date"))).encode("utf-8")
        ).hexdigest()[:24]
    if not operation_id:
        raise ValueError("operation_id is required for archive-stage")
    _check_archive_lease(connection, lock_token, operation_id)

    existing_operation = connection.execute(
        "SELECT project_id FROM archive_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if existing_operation and clean_text(existing_operation[0]) != project_id:
        raise ValueError("operation_id already belongs to another project")

    connection.execute(
        """
        INSERT INTO archive_operations(operation_id, project_id, recommendation_date,
                                       status, collection_key, collection_name,
                                       created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
        ON CONFLICT(operation_id) DO UPDATE SET
            collection_key=CASE WHEN excluded.collection_key != '' THEN excluded.collection_key ELSE archive_operations.collection_key END,
            collection_name=CASE WHEN excluded.collection_name != '' THEN excluded.collection_name ELSE archive_operations.collection_name END,
            updated_at=excluded.updated_at
        """,
        (operation_id, project_id, clean_text(data.get("recommendation_date")) or today(), collection_key, collection_name, now_iso(), now_iso()),
    )
    if collection_key or collection_name:
        connection.execute(
            """
            UPDATE projects SET
                collection_key=CASE WHEN ? != '' THEN ? ELSE collection_key END,
                collection_name=CASE WHEN ? != '' THEN ? ELSE collection_name END,
                zotero_library_id=CASE WHEN ? > 0 THEN ? ELSE zotero_library_id END,
                updated_at=?
            WHERE project_id=?
            """,
            (collection_key, collection_key, collection_name, collection_name, library_id, library_id, now_iso(), project_id),
        )

    updated: list[dict[str, Any]] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            raise ValueError("each archive result must be an object")
        work_id, match_basis = _resolve_archive_work_id(connection, project_id, raw_result)
        stage = _normalize_stage(raw_result.get("stage") or default_stage)
        status = _validate_status(raw_result.get("status") or "pending")
        canonical_id = clean_text(raw_result.get("canonical_id")) or _canonical_for_work(connection, project_id, work_id)
        item_key = clean_text(raw_result.get("zotero_item_key") or raw_result.get("item_key"))
        note_key = clean_text(raw_result.get("zotero_note_key") or raw_result.get("note_key"))
        item_action = clean_text(raw_result.get("item_action"))
        error = clean_text(raw_result.get("error") or raw_result.get("last_error"))
        old_stage = connection.execute(
            """
            SELECT attempt_count, collection_key, collection_name, zotero_item_key,
                   zotero_note_key, item_action, error, result_json
            FROM archive_stages WHERE operation_id=? AND work_id=? AND stage=?
            """,
            (operation_id, work_id, stage),
        ).fetchone()
        attempt_count = int(old_stage[0]) + 1 if old_stage else 1
        stage_collection_key = collection_key or (clean_text(old_stage[1]) if old_stage else "")
        stage_collection_name = collection_name or (clean_text(old_stage[2]) if old_stage else "")
        item_key = item_key or (clean_text(old_stage[3]) if old_stage else "")
        note_key = note_key or (clean_text(old_stage[4]) if old_stage else "")
        item_action = item_action or (clean_text(old_stage[5]) if old_stage else "")
        error = error or (clean_text(old_stage[6]) if old_stage else "")
        result_json = _json(raw_result)
        connection.execute(
            """
            INSERT INTO archive_stages(
                operation_id, project_id, work_id, stage, status, item_action,
                collection_key, collection_name, zotero_item_key, zotero_note_key,
                error, attempt_count, result_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operation_id, work_id, stage) DO UPDATE SET
                status=excluded.status,
                item_action=CASE WHEN excluded.item_action != '' THEN excluded.item_action ELSE archive_stages.item_action END,
                collection_key=CASE WHEN excluded.collection_key != '' THEN excluded.collection_key ELSE archive_stages.collection_key END,
                collection_name=CASE WHEN excluded.collection_name != '' THEN excluded.collection_name ELSE archive_stages.collection_name END,
                zotero_item_key=CASE WHEN excluded.zotero_item_key != '' THEN excluded.zotero_item_key ELSE archive_stages.zotero_item_key END,
                zotero_note_key=CASE WHEN excluded.zotero_note_key != '' THEN excluded.zotero_note_key ELSE archive_stages.zotero_note_key END,
                error=excluded.error, attempt_count=excluded.attempt_count,
                result_json=excluded.result_json, updated_at=excluded.updated_at
            """,
            (operation_id, project_id, work_id, stage, status, item_action, stage_collection_key, stage_collection_name, item_key, note_key, error, attempt_count, result_json, now_iso()),
        )
        old_state = connection.execute(
            """
            SELECT status, attempt_count, collection_key, collection_name,
                   zotero_item_key, zotero_note_key, item_action, last_error
            FROM archive_state WHERE project_id=? AND work_id=?
            """,
            (project_id, work_id),
        ).fetchone()
        state_status = _archive_state_status(clean_text(old_state[0]) if old_state else "", status, stage)
        state_attempt = (int(old_state[1]) if old_state else 0) + 1
        state_collection_key = stage_collection_key or (clean_text(old_state[2]) if old_state else "")
        state_collection_name = stage_collection_name or (clean_text(old_state[3]) if old_state else "")
        state_item_key = item_key or (clean_text(old_state[4]) if old_state else "")
        state_note_key = note_key or (clean_text(old_state[5]) if old_state else "")
        state_item_action = item_action or (clean_text(old_state[6]) if old_state else "")
        state_error = error if state_status != "saved" else ""
        connection.execute(
            """
            INSERT INTO archive_state(
                project_id, work_id, canonical_id, operation_id, status,
                collection_key, collection_name, zotero_item_key, zotero_note_key,
                item_action, last_error, attempt_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, work_id) DO UPDATE SET
                canonical_id=CASE WHEN excluded.canonical_id != '' THEN excluded.canonical_id ELSE archive_state.canonical_id END,
                operation_id=CASE WHEN excluded.operation_id != '' THEN excluded.operation_id ELSE archive_state.operation_id END,
                status=excluded.status,
                collection_key=CASE WHEN excluded.collection_key != '' THEN excluded.collection_key ELSE archive_state.collection_key END,
                collection_name=CASE WHEN excluded.collection_name != '' THEN excluded.collection_name ELSE archive_state.collection_name END,
                zotero_item_key=CASE WHEN excluded.zotero_item_key != '' THEN excluded.zotero_item_key ELSE archive_state.zotero_item_key END,
                zotero_note_key=CASE WHEN excluded.zotero_note_key != '' THEN excluded.zotero_note_key ELSE archive_state.zotero_note_key END,
                item_action=CASE WHEN excluded.item_action != '' THEN excluded.item_action ELSE archive_state.item_action END,
                last_error=excluded.last_error, attempt_count=excluded.attempt_count,
                updated_at=excluded.updated_at
            """,
            (project_id, work_id, canonical_id, operation_id, state_status, state_collection_key, state_collection_name, state_item_key, state_note_key, state_item_action, state_error, state_attempt, now_iso()),
        )
        _upsert_archive_compat(
            connection,
            project_id=project_id,
            work_id=work_id,
            canonical_id=canonical_id,
            status=state_status,
            collection_key=state_collection_key,
            collection_name=state_collection_name,
            item_key=state_item_key,
            note_key=state_note_key,
            error=state_error,
            attempt_count=state_attempt,
            operation_id=operation_id,
            stage=stage,
            item_action=state_item_action,
            result_json=result_json,
        )
        updated.append({"work_id": work_id, "canonical_id": canonical_id, "stage": stage, "status": state_status, "match_basis": match_basis, "attempt_count": attempt_count, "zotero_item_key": state_item_key, "zotero_note_key": state_note_key})

    operation_status = _aggregate_operation_status(connection, operation_id)
    connection.execute("UPDATE archive_operations SET status=?, updated_at=? WHERE operation_id=?", (operation_status, now_iso(), operation_id))
    return updated


def read_input(path: str, *, require_papers: bool = True) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(raw)
    if isinstance(value, list):
        value = {"papers": value}
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object or paper list")
    if require_papers and not isinstance(value.get("papers"), list):
        raise ValueError("input.papers must be a JSON list")
    return value


def command_history(args: argparse.Namespace) -> int:
    with open_db(Path(args.db)) as connection:
        rows = connection.execute("SELECT * FROM recommendations WHERE project_id=? ORDER BY recommended_at DESC LIMIT ?", (args.project_id, args.limit)).fetchall()
        recommendations: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            work_id = clean_text(item.get("work_id")) or clean_text(item.get("canonical_id"))
            item["work_id"] = work_id
            item["payload"] = _safe_payload(item.pop("payload_json", "{}"))
            item["identifiers"] = [dict(alias) for alias in connection.execute("SELECT identifier_type AS type, identifier_value AS value, verified, source FROM identifiers WHERE work_id=? ORDER BY identifier_type, identifier_value", (work_id,)).fetchall()]
            archive = connection.execute("SELECT * FROM archive_state WHERE project_id=? AND work_id=?", (args.project_id, work_id)).fetchone()
            item["archive"] = dict(archive) if archive else None
            recommendations.append(item)
    print(json.dumps({"project_id": args.project_id, "recommendations": recommendations}, ensure_ascii=False, indent=2))
    return 0


def command_check(args: argparse.Namespace) -> int:
    data = read_input(args.input)
    project_id, _, _ = _project_values(data)
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    with open_db(Path(args.db)) as connection:
        for raw_paper in data["papers"]:
            result = _candidate_check(connection, project_id, raw_paper, seen)
            seen.add(result["work_id"])
            results.append(result)
    print(json.dumps({"project_id": project_id, "results": results, "already_recommended": [result["work_id"] for result in results if result["already_recommended"]], "new": [result["work_id"] for result in results if not result["already_recommended"]]}, ensure_ascii=False, indent=2))
    return 0


def command_record(args: argparse.Namespace) -> int:
    data = read_input(args.input)
    project_id, project_name, project_path = _project_values(data)
    recommendation_date = clean_text(data.get("recommendation_date")) or today()
    raw_papers = data["papers"]
    if not raw_papers:
        raise ValueError("input.papers must contain at least one paper")

    with locked_db(Path(args.db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            project_result = upsert_project(
                connection,
                data,
                library_id=data.get("zotero_library_id") or data.get("library_id"),
            )
            _ensure_project_ready(project_result)
            prepared: list[tuple[dict[str, Any], str, str]] = []
            for raw_paper in raw_papers:
                if not isinstance(raw_paper, dict):
                    raise ValueError("each item in papers must be an object")
                if not clean_text(raw_paper.get("title")):
                    raise ValueError("paper.title is required")
                work_id, basis = _resolve_work_id(connection, raw_paper, create=True)
                prepared.append((raw_paper, work_id, basis))

            work_ids = [work_id for _, work_id, _ in prepared]
            operation_id = _make_operation_id(project_id, recommendation_date, data, work_ids)
            run_id = _make_run_id(project_id, recommendation_date, data, work_ids)
            recorded: list[str] = []
            already_present: list[str] = []
            already_recommended: list[str] = []
            duplicate_batch: list[str] = []
            inserted_work_ids: list[str] = []
            seen_in_batch: set[str] = set()

            for raw_paper, work_id, _ in prepared:
                canonical_id = canonical_id_for(raw_paper)
                if work_id in seen_in_batch:
                    duplicate_batch.append(canonical_id)
                    continue
                seen_in_batch.add(work_id)
                existing = connection.execute("SELECT recommendation_id, recommended_on, operation_id FROM recommendations WHERE project_id=? AND (work_id=? OR (work_id='' AND canonical_id=?)) ORDER BY recommended_at DESC LIMIT 1", (project_id, work_id, canonical_id)).fetchone()
                if existing:
                    if clean_text(existing["recommended_on"]) == recommendation_date:
                        already_present.append(canonical_id)
                    else:
                        already_recommended.append(canonical_id)
                    continue
                connection.execute("""
                    INSERT INTO recommendations(
                        project_id, work_id, canonical_id, title, doi, pmid, arxiv_id,
                        fallback_key, recommended_on, recommended_at, operation_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (project_id, work_id, canonical_id, clean_text(raw_paper.get("title")), normalize_doi(raw_paper.get("doi")), normalize_pmid(raw_paper.get("pmid")), normalize_arxiv(raw_paper.get("arxiv_id") or raw_paper.get("arxiv")), fallback_key(raw_paper), recommendation_date, now_iso(), operation_id, _json(paper_payload(raw_paper))))
                recorded.append(canonical_id)
                inserted_work_ids.append(work_id)

            final_work_ids = [work_id for _, work_id, _ in prepared]
            run_reused = not _record_run(connection, data, run_id=run_id, final_work_ids=final_work_ids, excluded_count=len(already_present) + len(already_recommended) + len(duplicate_batch))
            connection.execute("""
                INSERT INTO archive_operations(
                    operation_id, project_id, recommendation_date, status,
                    collection_key, collection_name, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', '', '', ?, ?)
                ON CONFLICT(operation_id) DO NOTHING
                """, (operation_id, project_id, recommendation_date, now_iso(), now_iso()))
            tasks: list[dict[str, Any]] = []
            for work_id in inserted_work_ids:
                canonical_id = _canonical_for_work(connection, project_id, work_id)
                connection.execute("""
                    INSERT INTO archive_state(
                        project_id, work_id, canonical_id, operation_id, status,
                        collection_key, collection_name, zotero_item_key, zotero_note_key,
                        item_action, last_error, attempt_count, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', '', '', '', '', '', '', 0, ?)
                    ON CONFLICT(project_id, work_id) DO NOTHING
                    """, (project_id, work_id, canonical_id, operation_id, now_iso()))
                connection.execute("""
                    INSERT INTO archive_stages(
                        operation_id, project_id, work_id, stage, status, item_action,
                        collection_key, collection_name, zotero_item_key, zotero_note_key,
                        error, attempt_count, result_json, updated_at
                    ) VALUES (?, ?, ?, 'prepare', 'pending', '', '', '', '', '', '', 0, '{}', ?)
                    ON CONFLICT(operation_id, work_id, stage) DO NOTHING
                    """, (operation_id, project_id, work_id, now_iso()))
                tasks.append({"work_id": work_id, "canonical_id": canonical_id, "status": "pending"})
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    print(json.dumps({"project_id": project_id, "project_name": project_name, "project_path": project_path, "operation_id": operation_id, "run_id": run_id, "run_reused": run_reused, "recorded": recorded, "already_present_for_date": already_present, "already_recommended": already_recommended, "duplicate_in_batch": duplicate_batch, "archive_tasks": tasks, "state_db": str(Path(args.db))}, ensure_ascii=False, indent=2))
    return 0


def command_run(args: argparse.Namespace) -> int:
    data = read_input(args.input, require_papers=False)
    project_id, _, _ = _project_values(data)
    run_date = clean_text(data.get("recommendation_date")) or today()
    candidate_ids = data.get("candidate_ids") if isinstance(data.get("candidate_ids"), list) else []
    work_ids = [clean_text(value) for value in candidate_ids if clean_text(value)]
    run_id = _make_run_id(project_id, run_date, data, work_ids)
    with locked_db(Path(args.db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            project_result = upsert_project(
                connection,
                data,
                library_id=data.get("zotero_library_id") or data.get("library_id"),
            )
            _ensure_project_ready(project_result)
            inserted = _record_run(connection, data, run_id=run_id, final_work_ids=[clean_text(value) for value in data.get("final_work_ids", [])] if isinstance(data.get("final_work_ids"), list) else [])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    print(json.dumps({"run_id": run_id, "recorded": inserted, "state_db": str(Path(args.db))}, ensure_ascii=False, indent=2))
    return 0


def command_archive_stage(args: argparse.Namespace) -> int:
    data = read_input(args.input, require_papers=False)
    project_id, _, _ = _project_values(data)
    with locked_db(Path(args.db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            updated = _apply_archive_results(connection, data, default_stage=clean_text(args.stage) or "complete", require_operation=True, lock_token=clean_text(args.lock_token))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    print(json.dumps({"project_id": project_id, "operation_id": clean_text(data.get("operation_id")), "updated": updated, "state_db": str(Path(args.db))}, ensure_ascii=False, indent=2))
    return 0


def command_archive_status(args: argparse.Namespace) -> int:
    """Compatibility adapter for the old one-shot archive-status command."""

    data = read_input(args.input)
    project_id, _, _ = _project_values(data)
    with locked_db(Path(args.db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            updated = _apply_archive_results(connection, data, default_stage="complete", require_operation=False)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    print(json.dumps({"project_id": project_id, "updated": updated, "state_db": str(Path(args.db))}, ensure_ascii=False, indent=2))
    return 0


def command_pending(args: argparse.Namespace) -> int:
    with open_db(Path(args.db)) as connection:
        states = connection.execute("SELECT * FROM archive_state WHERE project_id=? AND status != 'saved' ORDER BY updated_at DESC", (args.project_id,)).fetchall()
        pending: list[dict[str, Any]] = []
        for state_row in states:
            item = dict(state_row)
            payload = _paper_for_work(connection, args.project_id, clean_text(state_row["work_id"]))
            item["paper"] = payload
            item["title"] = clean_text(payload.get("title"))
            item["relevance_reason"] = clean_text(payload.get("relevance_reason"))
            item["reading_focus"] = clean_text(payload.get("reading_focus"))
            item["summary"] = clean_text(payload.get("summary"))
            pending.append(item)
    print(json.dumps({"project_id": args.project_id, "pending": pending}, ensure_ascii=False, indent=2))
    return 0


def command_project(args: argparse.Namespace) -> int:
    data = {"project_id": args.project_id, "project_name": args.project_name, "project_path": args.project_path}
    with locked_db(Path(args.db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = upsert_project(
                connection,
                data,
                rename_decision=args.rename_decision,
                collection_key=args.collection_key,
                collection_name=args.collection_name,
                library_id=args.library_id,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_project_map(args: argparse.Namespace) -> int:
    with open_db(Path(args.db)) as connection:
        row = _project_row(connection, args.project_id)
        decisions = [dict(item) for item in connection.execute("SELECT * FROM project_rename_decisions WHERE project_id=? ORDER BY decided_at DESC", (args.project_id,)).fetchall()]
    print(json.dumps({"project_id": args.project_id, "project": dict(row) if row else None, "rename_decisions": decisions}, ensure_ascii=False, indent=2))
    return 0


def command_archive_lock(args: argparse.Namespace) -> int:
    operation_id = clean_text(args.operation_id)
    lease_seconds = max(30, min(int(args.lease_seconds), 3600))
    token = secrets.token_urlsafe(24)
    expires = datetime.fromtimestamp(time.time() + lease_seconds, timezone.utc).isoformat(timespec="seconds")
    with locked_db(Path(args.db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            old = connection.execute("SELECT token, operation_id, expires_at FROM archive_mutex WHERE mutex_id=1").fetchone()
            if old and clean_text(old[2]) > now_iso() and clean_text(old[0]) != token:
                raise ValueError("another archive operation currently holds the local archive lock")
            connection.execute("""
                INSERT INTO archive_mutex(mutex_id, token, operation_id, expires_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(mutex_id) DO UPDATE SET token=excluded.token,
                    operation_id=excluded.operation_id, expires_at=excluded.expires_at
                """, (token, operation_id, expires))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    print(json.dumps({"lock_token": token, "operation_id": operation_id, "expires_at": expires}, ensure_ascii=False, indent=2))
    return 0


def command_archive_unlock(args: argparse.Namespace) -> int:
    with locked_db(Path(args.db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT token FROM archive_mutex WHERE mutex_id=1").fetchone()
            if not row:
                raise ValueError("archive lock not found")
            if clean_text(row[0]) != clean_text(args.lock_token):
                raise ValueError("archive lock token does not match")
            connection.execute("DELETE FROM archive_mutex WHERE mutex_id=1")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    print(json.dumps({"unlocked": True}, ensure_ascii=False, indent=2))
    return 0


def command_migrate(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    with open_db(db_path) as connection:
        row = connection.execute("SELECT value FROM state_meta WHERE key='last_migration_backup'").fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    print(json.dumps({"schema_version": version, "last_migration_backup": row[0] if row else "", "state_db": str(db_path)}, ensure_ascii=False, indent=2))
    return 0


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            project_name TEXT NOT NULL,
            project_path TEXT NOT NULL DEFAULT '',
            collection_key TEXT NOT NULL DEFAULT '',
            collection_name TEXT NOT NULL DEFAULT '',
            pending_project_name TEXT NOT NULL DEFAULT '',
            rename_pending INTEGER NOT NULL DEFAULT 0,
            zotero_library_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_rename_decisions (
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            old_name TEXT NOT NULL,
            new_name TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('keep', 'sync')),
            decided_at TEXT NOT NULL,
            PRIMARY KEY(project_id, old_name, new_name)
        );

        CREATE TABLE IF NOT EXISTS works (
            work_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            first_author TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            venue TEXT NOT NULL DEFAULT '',
            fallback_key TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identifiers (
            identifier_type TEXT NOT NULL,
            identifier_value TEXT NOT NULL,
            work_id TEXT NOT NULL REFERENCES works(work_id),
            verified INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(identifier_type, identifier_value)
        );
        CREATE INDEX IF NOT EXISTS idx_identifiers_work ON identifiers(work_id);

        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            run_date TEXT NOT NULL,
            queries_json TEXT NOT NULL DEFAULT '[]',
            sources_json TEXT NOT NULL DEFAULT '[]',
            candidate_ids_json TEXT NOT NULL DEFAULT '[]',
            selection_notes_json TEXT NOT NULL DEFAULT '{}',
            candidate_count INTEGER NOT NULL DEFAULT 0,
            excluded_count INTEGER NOT NULL DEFAULT 0,
            final_work_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            recommendation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            work_id TEXT NOT NULL DEFAULT '',
            canonical_id TEXT NOT NULL,
            title TEXT NOT NULL,
            doi TEXT NOT NULL DEFAULT '',
            pmid TEXT NOT NULL DEFAULT '',
            arxiv_id TEXT NOT NULL DEFAULT '',
            fallback_key TEXT NOT NULL DEFAULT '',
            recommended_on TEXT NOT NULL,
            recommended_at TEXT NOT NULL,
            operation_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            UNIQUE(project_id, canonical_id, recommended_on)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendations_project ON recommendations(project_id, recommended_at DESC);

        CREATE TABLE IF NOT EXISTS archive_operations (
            operation_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            recommendation_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'partial', 'saved', 'failed', 'needs_input')),
            collection_key TEXT NOT NULL DEFAULT '',
            collection_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_state (
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            work_id TEXT NOT NULL REFERENCES works(work_id),
            canonical_id TEXT NOT NULL DEFAULT '',
            operation_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'partial', 'saved', 'failed', 'needs_input')),
            collection_key TEXT NOT NULL DEFAULT '',
            collection_name TEXT NOT NULL DEFAULT '',
            zotero_item_key TEXT NOT NULL DEFAULT '',
            zotero_note_key TEXT NOT NULL DEFAULT '',
            item_action TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(project_id, work_id)
        );
        CREATE TABLE IF NOT EXISTS archive_stages (
            operation_id TEXT NOT NULL REFERENCES archive_operations(operation_id),
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            work_id TEXT NOT NULL REFERENCES works(work_id),
            stage TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'partial', 'saved', 'failed', 'needs_input')),
            item_action TEXT NOT NULL DEFAULT '',
            collection_key TEXT NOT NULL DEFAULT '',
            collection_name TEXT NOT NULL DEFAULT '',
            zotero_item_key TEXT NOT NULL DEFAULT '',
            zotero_note_key TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(operation_id, work_id, stage)
        );
        CREATE TABLE IF NOT EXISTS archive_status (
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            canonical_id TEXT NOT NULL,
            work_id TEXT NOT NULL DEFAULT '',
            operation_id TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT 'complete',
            item_action TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            collection_key TEXT NOT NULL DEFAULT '',
            collection_name TEXT NOT NULL DEFAULT '',
            zotero_item_key TEXT NOT NULL DEFAULT '',
            zotero_note_key TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(project_id, canonical_id)
        );
        CREATE TABLE IF NOT EXISTS archive_mutex (
            mutex_id INTEGER PRIMARY KEY CHECK(mutex_id=1),
            token TEXT NOT NULL,
            operation_id TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL
        );
        """)


def _migrate_legacy_data(connection: sqlite3.Connection) -> None:
    """Populate durable work/alias/state tables without merging uncertain records."""

    if _table_exists(connection, "recommendations"):
        rows = connection.execute("SELECT * FROM recommendations").fetchall()
        columns = _columns(connection, "recommendations")
        for row in rows:
            row_dict = dict(row)
            payload = _safe_payload(row_dict.get("payload_json"))
            canonical = clean_text(row_dict.get("canonical_id"))
            work_id = clean_text(row_dict.get("work_id")) or canonical
            if not work_id:
                work_id = work_id_for(payload)
            if not payload.get("title"):
                payload["title"] = clean_text(row_dict.get("title"))
            try:
                _insert_work(connection, work_id, payload, created_at=clean_text(row_dict.get("recommended_at")) or now_iso())
            except ValueError:
                continue
            for alias in _aliases_from_legacy(canonical, payload):
                existing = connection.execute("SELECT work_id FROM identifiers WHERE identifier_type=? AND identifier_value=?", (alias["type"], alias["value"])).fetchone()
                if existing and existing[0] != work_id:
                    continue
                connection.execute("""
                    INSERT OR IGNORE INTO identifiers(
                        identifier_type, identifier_value, work_id, verified, source,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (alias["type"], alias["value"], work_id, int(bool(alias["verified"])), alias.get("source", "legacy"), clean_text(row_dict.get("recommended_at")) or now_iso(), now_iso()))
            if "work_id" in columns and "fallback_key" in columns:
                connection.execute("UPDATE recommendations SET work_id=?, fallback_key=CASE WHEN fallback_key='' THEN ? ELSE fallback_key END WHERE recommendation_id=?", (work_id, fallback_key(payload) if payload.get("title") else "", row_dict["recommendation_id"]))
            elif "work_id" in columns:
                connection.execute("UPDATE recommendations SET work_id=? WHERE recommendation_id=?", (work_id, row_dict["recommendation_id"]))

    if _table_exists(connection, "archive_status"):
        rows = connection.execute("SELECT * FROM archive_status").fetchall()
        columns = _columns(connection, "archive_status")
        for row in rows:
            item = dict(row)
            project_id = clean_text(item.get("project_id"))
            canonical = clean_text(item.get("canonical_id"))
            work_id = clean_text(item.get("work_id")) or canonical
            if not work_id:
                continue
            rec = connection.execute("SELECT work_id FROM recommendations WHERE project_id=? AND canonical_id=? LIMIT 1", (project_id, canonical)).fetchone()
            work_id = clean_text(rec[0]) if rec and clean_text(rec[0]) else work_id
            if not connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone():
                _insert_work(connection, work_id, {"title": canonical})
            connection.execute("""
                INSERT INTO archive_state(
                    project_id, work_id, canonical_id, operation_id, status,
                    collection_key, collection_name, zotero_item_key, zotero_note_key,
                    item_action, last_error, attempt_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, work_id) DO UPDATE SET
                    status=excluded.status, canonical_id=excluded.canonical_id,
                    collection_key=excluded.collection_key, collection_name=excluded.collection_name,
                    zotero_item_key=excluded.zotero_item_key, zotero_note_key=excluded.zotero_note_key,
                    last_error=excluded.last_error, attempt_count=excluded.attempt_count,
                    updated_at=excluded.updated_at
                """, (project_id, work_id, canonical, clean_text(item.get("operation_id")) if "operation_id" in columns else "", _validate_status(item.get("status") or "pending"), clean_text(item.get("collection_key")), clean_text(item.get("collection_name")), clean_text(item.get("zotero_item_key")), clean_text(item.get("zotero_note_key")), clean_text(item.get("item_action")) if "item_action" in columns else "", clean_text(item.get("last_error")), int(item.get("attempt_count") or 0), clean_text(item.get("updated_at")) or now_iso()))


def _ensure_schema(connection: sqlite3.Connection, db_path: Path) -> None:
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    had_user_data = _has_user_tables(connection)
    backup_path = ""
    if current < SCHEMA_VERSION and had_user_data and db_path.exists() and db_path.stat().st_size:
        backup_path = _backup_before_migration(connection, db_path)

    _create_schema(connection)
    for table, name, definition in (
        ("projects", "pending_project_name", "TEXT NOT NULL DEFAULT ''"),
        ("projects", "rename_pending", "INTEGER NOT NULL DEFAULT 0"),
        ("projects", "zotero_library_id", "INTEGER NOT NULL DEFAULT 0"),
        ("recommendations", "work_id", "TEXT NOT NULL DEFAULT ''"),
        ("recommendations", "fallback_key", "TEXT NOT NULL DEFAULT ''"),
        ("recommendations", "operation_id", "TEXT NOT NULL DEFAULT ''"),
        ("archive_status", "work_id", "TEXT NOT NULL DEFAULT ''"),
        ("archive_status", "operation_id", "TEXT NOT NULL DEFAULT ''"),
        ("archive_status", "stage", "TEXT NOT NULL DEFAULT 'complete'"),
        ("archive_status", "item_action", "TEXT NOT NULL DEFAULT ''"),
        ("archive_status", "result_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        _add_column(connection, table, name, definition)
    # The v4 work index must be created after old databases receive work_id.
    connection.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_work ON recommendations(project_id, work_id, recommended_at DESC)")

    if current < SCHEMA_VERSION:
        _migrate_legacy_data(connection)
        if backup_path:
            connection.execute("INSERT INTO state_meta(key, value) VALUES('last_migration_backup', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (backup_path,))
        connection.execute("PRAGMA user_version = 4")
    connection.commit()


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with file_mutex(_state_lock_path(db_path)):
        connection = sqlite3.connect(db_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
            _ensure_schema(connection, db_path)
        except Exception:
            connection.close()
            raise
    return connection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(default_state_dir() / DEFAULT_DB_NAME), help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    history = subparsers.add_parser("history", help="List recommendation history")
    history.add_argument("--project-id", required=True)
    history.add_argument("--limit", type=int, default=1000)
    history.set_defaults(handler=command_history)

    check = subparsers.add_parser("check", help="Batch-check candidate identity and project history")
    check.add_argument("--input", required=True)
    check.set_defaults(handler=command_check)

    record = subparsers.add_parser("record", help="Record a recommendation batch atomically")
    record.add_argument("--input", required=True)
    record.set_defaults(handler=command_record)

    run = subparsers.add_parser("run", aliases=["record-run"], help="Record retrieval and selection audit data")
    run.add_argument("--input", required=True)
    run.set_defaults(handler=command_run)

    archive = subparsers.add_parser("archive-stage", help="Record one Zotero archive stage")
    archive.add_argument("--input", required=True)
    archive.add_argument("--stage", default="")
    archive.add_argument("--lock-token", default="")
    archive.set_defaults(handler=command_archive_stage)

    legacy_archive = subparsers.add_parser("archive-status", help="Compatibility alias for a complete archive result")
    legacy_archive.add_argument("--input", required=True)
    legacy_archive.set_defaults(handler=command_archive_status)

    pending = subparsers.add_parser("pending", help="List unfinished Zotero tasks with paper metadata")
    pending.add_argument("--project-id", required=True)
    pending.set_defaults(handler=command_pending)

    project = subparsers.add_parser("project", help="Update project and collection mapping")
    project.add_argument("--project-id", required=True)
    project.add_argument("--project-name", required=True)
    project.add_argument("--project-path", default="")
    project.add_argument("--collection-key", default="")
    project.add_argument("--collection-name", default="")
    project.add_argument("--library-id", type=int, default=0)
    project.add_argument("--rename-decision", choices=["keep", "sync"], default="")
    project.set_defaults(handler=command_project)

    project_map = subparsers.add_parser("project-map", help="Read a project collection mapping without changing it")
    project_map.add_argument("--project-id", required=True)
    project_map.set_defaults(handler=command_project_map)

    lock = subparsers.add_parser("archive-lock", help="Acquire a lease for a multi-call archive workflow")
    lock.add_argument("--operation-id", default="")
    lock.add_argument("--lease-seconds", type=int, default=300)
    lock.set_defaults(handler=command_archive_lock)

    unlock = subparsers.add_parser("archive-unlock", help="Release an archive lease")
    unlock.add_argument("--lock-token", required=True)
    unlock.set_defaults(handler=command_archive_unlock)

    migrate = subparsers.add_parser("migrate", help="Initialize or report the state schema")
    migrate.set_defaults(handler=command_migrate)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
