from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import recommendation_state as state


class RecommendationStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "recommendations.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = state.main(["--db", str(self.db), *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def write_input(self, value: dict) -> Path:
        path = Path(self.temp_dir.name) / f"input-{len(list(Path(self.temp_dir.name).glob('input-*.json')))}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def base_input(self) -> dict:
        return {
            "project_id": "project-1",
            "project_name": "Example Project",
            "project_path": "/tmp/example-project",
            "recommendation_date": "2026-09-08",
            "papers": [
                {
                    "title": "A Useful Paper",
                    "authors": ["Ada Lovelace"],
                    "year": 2025,
                    "doi": "https://doi.org/10.1234/Example.",
                    "relevance_reason": "Useful method",
                }
            ],
        }

    def test_identifier_normalization(self) -> None:
        paper = self.base_input()["papers"][0]
        self.assertEqual(state.paper_id(paper), "doi:10.1234/example")
        self.assertEqual(
            state.paper_id({"title": "A Useful-Paper!", "year": 2025}),
            "fallback:title:a useful paper|author:|year:2025|venue:",
        )

    def test_record_is_idempotent_for_same_date_and_history_is_readable(self) -> None:
        path = self.write_input(self.base_input())
        code, output, error = self.call(["record", "--input", str(path)])
        self.assertEqual(code, 0, error)
        self.assertEqual(len(json.loads(output)["recorded"]), 1)

        code, output, error = self.call(["record", "--input", str(path)])
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual(result["recorded"], [])
        self.assertEqual(result["already_present_for_date"], ["doi:10.1234/example"])

        code, output, error = self.call(["history", "--project-id", "project-1"])
        self.assertEqual(code, 0, error)
        history = json.loads(output)["recommendations"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["canonical_id"], "doi:10.1234/example")
        self.assertEqual(history[0]["payload"]["title"], "A Useful Paper")

    def test_archive_failure_is_separate_and_retryable(self) -> None:
        recommendation = self.base_input()
        recommendation_path = self.write_input(recommendation)
        self.assertEqual(self.call(["record", "--input", str(recommendation_path)])[0], 0)

        archive_result = dict(recommendation)
        archive_result["archive"] = {
            "collection": {"name": "Example Project", "key": "COLL1"},
            "papers": [
                {
                    "canonical_id": "doi:10.1234/example",
                    "status": "failed",
                    "error": "Zotero MCP unavailable",
                }
            ],
        }
        archive_path = self.write_input(archive_result)
        code, _, error = self.call(["archive-status", "--input", str(archive_path)])
        self.assertEqual(code, 0, error)

        code, output, error = self.call(["pending", "--project-id", "project-1"])
        self.assertEqual(code, 0, error)
        pending = json.loads(output)["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["attempt_count"], 1)

        archive_result["archive"]["papers"][0] = {
            "canonical_id": "doi:10.1234/example",
            "status": "saved",
            "zotero_item_key": "ITEM1",
            "zotero_note_key": "NOTE1",
        }
        archive_path.write_text(json.dumps(archive_result), encoding="utf-8")
        code, _, error = self.call(["archive-status", "--input", str(archive_path)])
        self.assertEqual(code, 0, error)

        code, output, error = self.call(["pending", "--project-id", "project-1"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["pending"], [])

    def test_identifier_normalization_keeps_balanced_doi_parentheses_and_decodes_urls(self) -> None:
        self.assertEqual(
            state.normalize_doi("https://doi.org/10.5555/abc%28supplement%29."),
            "10.5555/abc(supplement)",
        )
        self.assertEqual(state.normalize_doi("doi:10.5555/abc(supplement)"), "10.5555/abc(supplement)")
        self.assertEqual(state.normalize_doi("10.5555/abc)."), "10.5555/abc")
        self.assertEqual(state.normalize_pmid("PMID: 31452104"), "31452104")
        self.assertEqual(state.normalize_pmid("https://pubmed.ncbi.nlm.nih.gov/31452104/"), "31452104")
        self.assertEqual(state.normalize_arxiv("arXiv:2304.00464v2"), "2304.00464")
        self.assertEqual(state.normalize_arxiv("https://arxiv.org/abs/2304.00464v3"), "2304.00464")
        self.assertEqual(state.normalize_arxiv("https://arxiv.org/pdf/2304.00464v1.pdf"), "2304.00464")

    def test_batch_check_uses_database_identity_instead_of_history_limit(self) -> None:
        first = self.base_input()
        first["papers"][0]["pmid"] = "31452104"
        first["papers"][0]["identifiers"] = {
            "arxiv": [{"value": "2304.00464v2", "verified": True, "source": "paper metadata"}]
        }
        first_path = self.write_input(first)
        self.assertEqual(self.call(["record", "--input", str(first_path)])[0], 0)

        candidate = {
            "project_id": "project-1",
            "project_name": "Example Project",
            "papers": [
                {
                    "title": "A differently formatted title",
                    "authors": ["Someone Else"],
                    "year": 2025,
                    "pmid": "PMID 31452104",
                }
            ],
        }
        candidate_path = self.write_input(candidate)
        code, output, error = self.call(["check", "--input", str(candidate_path)])
        self.assertEqual(code, 0, error)
        result = json.loads(output)["results"][0]
        self.assertTrue(result["already_recommended"])
        self.assertEqual(result["match_basis"], "pmid")
        self.assertEqual(result["identifiers"][0]["value"], "31452104")

    def test_same_title_and_year_with_different_authors_stays_separate(self) -> None:
        first = self.base_input()
        first["papers"][0] = {
            "title": "Same title",
            "authors": ["Author One"],
            "year": 2025,
            "venue": "Journal A",
        }
        first_path = self.write_input(first)
        self.assertEqual(self.call(["record", "--input", str(first_path)])[0], 0)

        second = dict(first)
        second["recommendation_date"] = "2026-09-08"
        second["papers"] = [{
            "title": "Same title",
            "authors": ["Author Two"],
            "year": 2025,
            "venue": "Journal A",
        }]
        second_path = self.write_input(second)
        code, output, error = self.call(["check", "--input", str(second_path)])
        self.assertEqual(code, 0, error)
        result = json.loads(output)["results"][0]
        self.assertFalse(result["already_recommended"])
        self.assertNotEqual(result["work_id"], state.work_id_for(first["papers"][0]))
        self.assertEqual(self.call(["record", "--input", str(second_path)])[0], 0)

        with state.open_db(self.db) as connection:
            count = connection.execute("SELECT COUNT(*) FROM recommendations WHERE project_id='project-1'").fetchone()[0]
        self.assertEqual(count, 2)

    def test_verified_preprint_final_alias_reuses_one_work_across_projects(self) -> None:
        preprint = self.base_input()
        preprint["papers"][0] = {
            "title": "Confirmed work",
            "authors": ["Researcher"],
            "year": 2024,
            "venue": "arXiv",
            "arxiv_id": "2401.01234v2",
        }
        preprint_path = self.write_input(preprint)
        code, output, error = self.call(["record", "--input", str(preprint_path)])
        self.assertEqual(code, 0, error)
        preprint_work_id = json.loads(output)["archive_tasks"][0]["work_id"]

        final = {
            "project_id": "project-2",
            "project_name": "Another Project",
            "recommendation_date": "2026-09-09",
            "papers": [{
                "title": "Confirmed work",
                "authors": ["Researcher"],
                "year": 2025,
                "venue": "Journal B",
                "doi": "10.5555/final-version",
                "verified_aliases": [{"type": "arxiv", "value": "2401.01234v2", "source": "publisher relation"}],
            }],
        }
        final_path = self.write_input(final)
        code, output, error = self.call(["record", "--input", str(final_path)])
        self.assertEqual(code, 0, error)
        final_work_id = json.loads(output)["archive_tasks"][0]["work_id"]
        self.assertEqual(final_work_id, preprint_work_id)

        with state.open_db(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM works").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0], 2)

    def test_unverified_alias_does_not_merge_a_preprint_with_a_final_paper(self) -> None:
        preprint = self.base_input()
        preprint["papers"][0] = {
            "title": "Unconfirmed work",
            "authors": ["Researcher"],
            "year": 2024,
            "arxiv_id": "2402.01234",
        }
        preprint_path = self.write_input(preprint)
        self.assertEqual(self.call(["record", "--input", str(preprint_path)])[0], 0)

        final = dict(preprint)
        final["recommendation_date"] = "2026-09-09"
        final["papers"] = [{
            "title": "Unconfirmed work",
            "authors": ["Researcher"],
            "year": 2025,
            "doi": "10.5555/unconfirmed-final",
            "identifiers": [{"type": "arxiv", "value": "2402.01234", "verified": False}],
        }]
        final_path = self.write_input(final)
        code, output, error = self.call(["check", "--input", str(final_path)])
        self.assertEqual(code, 0, error)
        result = json.loads(output)["results"][0]
        self.assertFalse(result["already_recommended"])
        self.assertNotEqual(result["work_id"], state.work_id_for(preprint["papers"][0]))

    def test_project_mapping_keeps_zotero_library_id(self) -> None:
        code, output, error = self.call([
            "project", "--project-id", "library-1", "--project-name", "Library project",
            "--collection-key", "COLL1", "--collection-name", "Library project", "--library-id", "987",
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["zotero_library_id"], 987)

    def test_project_rename_is_asked_once_and_decision_is_persisted(self) -> None:
        args = ["project", "--project-id", "rename-1", "--project-name", "Old Name", "--project-path", "/tmp/p"]
        code, _, error = self.call(args)
        self.assertEqual(code, 0, error)
        code, _, error = self.call(args + ["--collection-key", "COLL1", "--collection-name", "Old Name"])
        self.assertEqual(code, 0, error)

        renamed = ["project", "--project-id", "rename-1", "--project-name", "New Name", "--project-path", "/tmp/p"]
        code, output, error = self.call(renamed)
        self.assertEqual(code, 0, error)
        prompt = json.loads(output)
        self.assertTrue(prompt["rename_required"])
        self.assertEqual(prompt["rename"]["collection_key"], "COLL1")

        code, output, error = self.call(renamed + ["--rename-decision", "keep"])
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertFalse(result["rename_required"])
        self.assertEqual(result["collection_name"], "Old Name")
        code, output, error = self.call(renamed)
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["rename_required"])

        code, output, error = self.call(["project-map", "--project-id", "rename-1"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["rename_decisions"][0]["decision"], "keep")

    def test_record_persists_run_audit_and_archive_task_atomically(self) -> None:
        payload = self.base_input()
        payload.update({
            "queries": ["phase separation"],
            "sources": ["Crossref", "arXiv"],
            "candidate_ids": ["doi:10.1234/example", "arxiv:2304.00464"],
            "selection_notes": {"selection": "method transfer"},
        })
        path = self.write_input(payload)
        code, output, error = self.call(["record", "--input", str(path)])
        self.assertEqual(code, 0, error)
        operation_id = json.loads(output)["operation_id"]
        with state.open_db(self.db) as connection:
            run = connection.execute("SELECT * FROM runs").fetchone()
            self.assertEqual(json.loads(run["queries_json"]), ["phase separation"])
            self.assertEqual(json.loads(run["sources_json"]), ["Crossref", "arXiv"])
            self.assertEqual(connection.execute("SELECT operation_id FROM archive_operations").fetchone()[0], operation_id)
            self.assertEqual(connection.execute("SELECT status FROM archive_state").fetchone()[0], "pending")

    def test_archive_stages_recover_without_creating_new_task(self) -> None:
        path = self.write_input(self.base_input())
        code, output, error = self.call(["record", "--input", str(path)])
        self.assertEqual(code, 0, error)
        recorded = json.loads(output)
        operation_id = recorded["operation_id"]
        work_id = recorded["archive_tasks"][0]["work_id"]
        stage_input = {
            "project_id": "project-1",
            "project_name": "Example Project",
            "operation_id": operation_id,
            "archive": {"collection": {"name": "Example Project", "key": "COLL1"}, "papers": [{
                "work_id": work_id,
                "stage": "item",
                "status": "saved",
                "item_action": "created",
                "zotero_item_key": "ITEM1",
            }]},
        }
        item_path = self.write_input(stage_input)
        code, output, error = self.call(["archive-stage", "--input", str(item_path)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["updated"][0]["status"], "partial")
        code, output, error = self.call(["pending", "--project-id", "project-1"])
        self.assertEqual(code, 0, error)
        pending = json.loads(output)["pending"][0]
        self.assertEqual(pending["relevance_reason"], "Useful method")

        complete = dict(stage_input)
        complete["archive"] = {"collection": {"name": "Example Project", "key": "COLL1"}, "papers": [{
            "work_id": work_id,
            "stage": "complete",
            "status": "saved",
            "item_action": "reused",
            "zotero_item_key": "ITEM1",
            "zotero_note_key": "NOTE1",
        }]}
        complete_path = self.write_input(complete)
        code, _, error = self.call(["archive-stage", "--input", str(complete_path)])
        self.assertEqual(code, 0, error)
        code, _, error = self.call(["archive-stage", "--input", str(complete_path)])
        self.assertEqual(code, 0, error)
        with state.open_db(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM archive_state").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM archive_stages WHERE stage='complete'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT status FROM archive_state").fetchone()[0], "saved")

    def test_invalid_archive_state_is_rejected_without_partial_write(self) -> None:
        path = self.write_input(self.base_input())
        self.assertEqual(self.call(["record", "--input", str(path)])[0], 0)
        bad = self.base_input()
        bad["operation_id"] = "operation-bad"
        bad["archive"] = {"papers": [{"canonical_id": "doi:10.1234/example", "status": "wat"}]}
        bad_path = self.write_input(bad)
        code, _, error = self.call(["archive-stage", "--input", str(bad_path)])
        self.assertEqual(code, 2)
        self.assertIn("invalid archive status", error)
        with state.open_db(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM archive_stages WHERE operation_id='operation-bad'").fetchone()[0], 0)

    def test_archive_lease_serializes_retries(self) -> None:
        code, output, error = self.call(["archive-lock", "--operation-id", "op-1", "--lease-seconds", "60"])
        self.assertEqual(code, 0, error)
        token = json.loads(output)["lock_token"]
        code, _, error = self.call(["archive-lock", "--operation-id", "op-2", "--lease-seconds", "60"])
        self.assertEqual(code, 2)
        self.assertIn("another archive operation", error)
        code, _, error = self.call(["archive-unlock", "--lock-token", token])
        self.assertEqual(code, 0, error)

    def test_legacy_database_is_backed_up_and_migrated(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.executescript("""
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY, project_name TEXT NOT NULL,
                project_path TEXT NOT NULL DEFAULT '', collection_key TEXT NOT NULL DEFAULT '',
                collection_name TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
            );
            CREATE TABLE recommendations (
                recommendation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL, canonical_id TEXT NOT NULL, title TEXT NOT NULL,
                doi TEXT NOT NULL DEFAULT '', pmid TEXT NOT NULL DEFAULT '', arxiv_id TEXT NOT NULL DEFAULT '',
                recommended_on TEXT NOT NULL, recommended_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                UNIQUE(project_id, canonical_id, recommended_on)
            );
            CREATE TABLE archive_status (
                project_id TEXT NOT NULL, canonical_id TEXT NOT NULL,
                status TEXT NOT NULL, collection_key TEXT NOT NULL DEFAULT '',
                collection_name TEXT NOT NULL DEFAULT '', zotero_item_key TEXT NOT NULL DEFAULT '',
                zotero_note_key TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, canonical_id)
            );
        """)
        connection.execute("INSERT INTO projects VALUES ('legacy-1', 'Legacy', '/tmp/legacy', '', '', '2026-09-01T00:00:00+00:00')")
        connection.execute("INSERT INTO recommendations(project_id, canonical_id, title, recommended_on, recommended_at, payload_json) VALUES (?, ?, ?, ?, ?, ?)", ("legacy-1", "doi:10.5555/legacy", "Legacy paper", "2026-09-01", "2026-09-01T00:00:00+00:00", json.dumps({"title": "Legacy paper", "doi": "10.5555/legacy"})))
        connection.execute("INSERT INTO archive_status(project_id, canonical_id, status, collection_key, collection_name, zotero_item_key, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", ("legacy-1", "doi:10.5555/legacy", "saved", "COLL1", "Legacy", "ITEM1", "2026-09-01T00:00:00+00:00"))
        connection.commit()
        connection.close()

        code, output, error = self.call(["migrate"])
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual(result["schema_version"], state.SCHEMA_VERSION)
        self.assertTrue(Path(result["last_migration_backup"]).exists())
        with state.open_db(self.db) as migrated:
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0], 1)
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM works").fetchone()[0], 1)
            self.assertEqual(migrated.execute("SELECT identifier_value FROM identifiers").fetchone()[0], "10.5555/legacy")
            archive = migrated.execute("SELECT status, zotero_item_key FROM archive_state").fetchone()
            self.assertEqual(tuple(archive), ("saved", "ITEM1"))


if __name__ == "__main__":
    unittest.main()
