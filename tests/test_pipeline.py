from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from paper_marrow.cli import _vault, main
from paper_marrow.conversion import (
    ConversionCandidate,
    ConversionService,
    ConversionSummary,
)
from paper_marrow.files import (
    PipelineError,
    VaultLock,
    atomic_create,
    paper_folder_name,
    project_meta,
    read_meta,
    read_zotero_json,
    render_meta,
    render_zotero_json,
)
from paper_marrow.ingest import Importer
from paper_marrow.library import (
    Config,
    Pdf2MdConfig,
    index_existing,
    initialize,
    load_config,
)
from paper_marrow.pdf_links import (
    PdfLinkSummary,
    PdfLinker,
    file_uri_path,
    pdf_link_name,
)
from paper_marrow.pdf2md import (
    ConversionError,
    ConversionOptions,
    _staging_mode,
    convert_pdf,
    convert_pdf_into_paper,
    load_vault_token,
    organize_result,
)
from paper_marrow.zotero import Zotero, key, validate_url


def item(item_key: str = "PAPER001", *, title: str = "中文论文：测试", doi: str = "10.1234/example") -> dict:
    data = {
        "key": item_key,
        "version": 1,
        "itemType": "journalArticle",
        "title": title,
        "creators": [
            {"creatorType": "author", "firstName": "A", "lastName": "Researcher"},
            {"creatorType": "editor", "name": "Example Institute"},
        ],
        "abstractNote": "",
        "publicationTitle": "Example Journal",
        "volume": "",
        "issue": "001",
        "date": "2025-01-02",
        "DOI": doi,
        "accepted": False,
        "missing": None,
        "tags": [{"tag": "zotero-tag", "type": 1}],
        "collections": ["COLLECT1"],
        "relations": {"dc:relation": ["x", "y"]},
        "dateAdded": "2026-09-08T10:00:00Z",
        "dateModified": "2026-09-08T10:00:00Z",
        "futureField": {"nested": [1, 1.5, True, None]},
    }
    return {"key": item_key, "version": 1, "data": data}


def item_folder(vault: Path, entry: dict | None = None) -> Path:
    entry = item() if entry is None else entry
    return vault / paper_folder_name(entry["data"]["key"], entry["data"])


class FakeZotero:
    def __init__(
        self,
        entries: list[dict] | None = None,
        children: dict[str, list[dict]] | None = None,
        file_urls: dict[str, str] | None = None,
    ):
        self.entries = [item()] if entries is None else entries
        self.children_by_parent = {} if children is None else children
        self.file_urls = {} if file_urls is None else file_urls

    def items(self, _collection: str) -> list[dict]:
        return self.entries

    def new_session(self):
        return FakeZotero(self.entries, self.children_by_parent, self.file_urls)

    def children(self, parent_key: str) -> list[dict]:
        return self.children_by_parent.get(parent_key, [])

    def attachment_file_url(self, attachment_key: str) -> str:
        return self.file_urls[attachment_key]


def pdf_attachment(
    attachment_key: str,
    parent_key: str = "PAPER001",
    filename: str = "paper.pdf",
) -> dict:
    data = {
        "key": attachment_key,
        "version": 1,
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "imported_file",
        "title": "PDF",
        "contentType": "application/pdf",
        "filename": filename,
    }
    return {"key": attachment_key, "version": 1, "data": data}


class FakeMinerUResult:
    state = "done"
    error = None

    def __init__(self, *, include_full: bool = True):
        self.include_full = include_full

    def save_all(self, directory: str) -> None:
        root = Path(directory)
        if self.include_full:
            (root / "full.md").write_text("# Converted\n", encoding="utf-8")
        (root / "content_list.md").write_text("auxiliary", encoding="utf-8")
        (root / "paper_origin.pdf").write_bytes(b"%PDF-returned-copy")
        (root / "layout.json").write_text("{}", encoding="utf-8")
        images = root / "images"
        images.mkdir()
        (images / "figure.png").write_bytes(b"png")


class FakeMinerUClient:
    def __init__(self, token: str, result: FakeMinerUResult | None = None):
        self.token = token
        self.result = result or FakeMinerUResult()
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def extract(self, pdf: str, **options):
        self.calls.append((pdf, options))
        return self.result


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "论文 vault"
        self.vault.mkdir()
        self.config = Config("COLLECT1", "http://localhost:23119/api/")

    def sync(self, entries: list[dict] | None = None):
        return Importer(self.vault, self.config, FakeZotero(entries)).sync()

    def test_complete_data_is_written_directly_under_vault(self):
        source = item()
        summary = self.sync([source])
        folder = item_folder(self.vault, source)
        path = folder / "meta.md"
        sidecar = folder / "temp" / "zotero-item.json"
        self.assertEqual(summary.created, 1)
        self.assertTrue(path.is_file())
        self.assertTrue(sidecar.is_file())
        self.assertFalse((folder / "zotero-item.json").exists())
        self.assertEqual(folder.name, "中文论文：测试 [PAPER001]")
        self.assertFalse((self.vault / "papers").exists())
        self.assertEqual(read_zotero_json(sidecar), source["data"])
        self.assertEqual(read_meta(path), project_meta(source["data"]))
        self.assertEqual(read_meta(path)["creators"], ["A Researcher", "Example Institute"])
        self.assertEqual(read_meta(path)["zoteroTags"], ["zotero-tag"])
        self.assertNotIn("tags", read_meta(path))
        self.assertEqual(read_meta(path)["relations"], source["data"]["relations"])
        self.assertEqual(path.read_text(encoding="utf-8").count("---"), 2)

    def test_render_preserves_empty_nested_unknown_and_string_types(self):
        data = item()["data"] | {
            "emptyList": [], "emptyObject": {}, "emptyString": "",
            "yesText": "yes", "numberText": "00123", "colonText": "a: b",
            "separatorText": "first line\n---\nlast line",
        }
        rendered = render_meta(data)
        loaded = yaml.safe_load(rendered[4:rendered.rfind("\n---\n")])
        expected = project_meta(data)
        self.assertEqual(loaded, expected)
        for name in ("date", "dateAdded", "yesText", "numberText", "colonText"):
            self.assertIs(type(loaded[name]), str)
        self.assertIs(type(loaded["accepted"]), bool)
        self.assertIsNone(loaded["missing"])

        path = self.vault / "with separator" / "meta.md"
        path.parent.mkdir()
        path.write_text(rendered, encoding="utf-8")
        self.assertEqual(read_meta(path), expected)

    def test_zotero_json_roundtrip_preserves_complete_data(self):
        data = item()["data"] | {
            "emptyList": [], "emptyObject": {}, "emptyString": "", "unicode": "论文",
        }
        path = self.vault / "PAPER001" / "temp" / "zotero-item.json"
        path.parent.mkdir(parents=True)
        path.write_text(render_zotero_json(data), encoding="utf-8")
        self.assertEqual(read_zotero_json(path), data)

    def test_sparse_short_title_is_not_synthesized(self):
        without = item("PAPER001")
        with_short = item("PAPER002")
        with_short["data"]["shortTitle"] = "A short title"
        summary = self.sync([without, with_short])
        self.assertEqual(summary.created, 2)
        self.assertNotIn("shortTitle", read_meta(item_folder(self.vault, without) / "meta.md"))
        self.assertEqual(
            read_meta(item_folder(self.vault, with_short) / "meta.md")["shortTitle"], "A short title"
        )
        self.assertEqual(item_folder(self.vault, with_short).name, "A short title [PAPER002]")

    def test_folder_name_falls_back_sanitizes_and_truncates(self):
        self.assertEqual(
            paper_folder_name("PAPER001", {"shortTitle": "  ", "title": "  A:\tB / C? .  "}),
            "A - B - C [PAPER001]",
        )
        self.assertEqual(
            paper_folder_name("PAPER001", {"title": 'A<>:"/\\|?*B'}),
            "A - B [PAPER001]",
        )
        self.assertEqual(paper_folder_name("PAPER001", {"title": ""}), "PAPER001")
        self.assertEqual(paper_folder_name("PAPER001", {}), "PAPER001")
        long_name = paper_folder_name("PAPER001", {"title": "文" * 120})
        self.assertEqual(long_name, f"{'文' * 100} [PAPER001]")
        with self.assertRaises(PipelineError):
            paper_folder_name("PAPER001", {"shortTitle": 123, "title": "fallback"})
        with self.assertRaises(PipelineError):
            paper_folder_name("PAPER001", {"shortTitle": "valid", "title": 123})

    def test_creator_and_tag_projection_preserves_display_values(self):
        entry = item()
        entry["data"]["creators"] = [
            {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
            {"creatorType": "author", "name": "研究机构"},
        ]
        entry["data"]["tags"] = [
            {"tag": "machine learning", "type": 1},
            {"tag": "ML", "type": 0},
            {"tag": "machine learning", "type": 1},
        ]
        self.sync([entry])
        folder = item_folder(self.vault, entry)
        meta = read_meta(folder / "meta.md")
        self.assertEqual(meta["creators"], ["Ada Lovelace", "研究机构"])
        self.assertEqual(meta["zoteroTags"], ["machine learning", "ML", "machine learning"])
        self.assertNotIn("tags", meta)
        self.assertEqual(
            read_zotero_json(folder / "temp" / "zotero-item.json"), entry["data"]
        )

    def test_repeat_sync_never_changes_existing_meta(self):
        self.sync()
        folder = item_folder(self.vault)
        path = folder / "meta.md"
        sidecar = folder / "temp" / "zotero-item.json"
        edited_meta = path.read_bytes() + b"user body\n"
        original_json = sidecar.read_bytes()
        path.write_bytes(edited_meta)
        changed = item(title="Zotero changed the title")
        summary = self.sync([changed])
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(path.read_bytes(), edited_meta)
        self.assertEqual(sidecar.read_bytes(), original_json)

    def test_completed_item_does_not_read_edited_sidecar(self):
        self.sync()
        folder = item_folder(self.vault)
        sidecar = folder / "temp" / "zotero-item.json"
        sidecar.write_text("not json", encoding="utf-8")
        summary = self.sync()
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "not json")

    def test_existing_meta_without_sidecar_is_skipped_without_backfill(self):
        folder = self.vault / "renamed"
        folder.mkdir()
        meta = folder / "meta.md"
        meta.write_text(render_meta(item()["data"]), encoding="utf-8")
        summary = self.sync()
        self.assertEqual(summary.skipped, 1)
        self.assertFalse((folder / "temp" / "zotero-item.json").exists())

    def test_existing_target_meta_with_another_key_gets_no_sidecar(self):
        folder = item_folder(self.vault)
        folder.mkdir()
        (folder / "meta.md").write_text(render_meta(item("OTHER001")["data"]), encoding="utf-8")
        summary = self.sync()
        self.assertEqual(summary.skipped, 1)
        self.assertFalse((folder / "temp" / "zotero-item.json").exists())

    def test_sidecar_only_interrupted_import_recovers_from_saved_snapshot(self):
        original = item(title="original snapshot")["data"]
        folder = self.vault / paper_folder_name("PAPER001", original)
        folder.mkdir()
        sidecar = folder / "temp" / "zotero-item.json"
        atomic_create(sidecar, render_zotero_json(original))
        original_bytes = sidecar.read_bytes()

        summary = self.sync([item(title="later Zotero value")])

        self.assertEqual(summary.created, 1)
        self.assertEqual(read_meta(folder / "meta.md"), project_meta(original))
        self.assertEqual(sidecar.read_bytes(), original_bytes)
        self.assertFalse(item_folder(self.vault, item(title="later Zotero value")).exists())

    def test_root_sidecar_is_not_used_by_the_new_layout(self):
        entry = item()
        folder = item_folder(self.vault, entry)
        folder.mkdir()
        stale = entry["data"] | {"abstractNote": "stale root snapshot"}
        root_sidecar = folder / "zotero-item.json"
        root_sidecar.write_text(render_zotero_json(stale), encoding="utf-8")

        summary = self.sync([entry])

        self.assertEqual(summary.created, 1)
        self.assertEqual(read_meta(folder / "meta.md"), project_meta(entry["data"]))
        self.assertEqual(
            read_zotero_json(folder / "temp" / "zotero-item.json"), entry["data"]
        )
        self.assertEqual(read_zotero_json(root_sidecar), stale)

    def test_invalid_existing_sidecar_does_not_create_meta(self):
        folder = self.vault / "PAPER001"
        folder.mkdir()
        sidecar = folder / "temp" / "zotero-item.json"
        sidecar.parent.mkdir()
        sidecar.write_text('{"key":"PAPER001","itemType":42}', encoding="utf-8")
        summary = self.sync()
        self.assertEqual(summary.failed, 1)
        self.assertFalse((folder / "meta.md").exists())

    def test_invalid_creator_or_tag_does_not_publish_partial_item(self):
        for field, invalid in (("creators", [{"creatorType": "author"}]), ("tags", [{"type": 1}])):
            with self.subTest(field=field):
                entry = item()
                entry["data"][field] = invalid
                summary = self.sync([entry])
                self.assertEqual(summary.failed, 1)
                self.assertFalse(item_folder(self.vault, entry).exists())

    def test_renamed_direct_child_folder_is_still_recognized(self):
        self.sync()
        item_folder(self.vault).rename(self.vault / "my paper")
        summary = self.sync()
        self.assertEqual(summary.skipped, 1)
        self.assertFalse(item_folder(self.vault).exists())

    def test_same_doi_with_different_keys_is_imported_twice(self):
        first, second = item("PAPER001"), item("PAPER002")
        summary = self.sync([first, second])
        self.assertEqual(summary.created, 2)
        self.assertTrue((item_folder(self.vault, first) / "meta.md").is_file())
        self.assertTrue((item_folder(self.vault, second) / "meta.md").is_file())
        self.assertNotEqual(item_folder(self.vault, first), item_folder(self.vault, second))

    def test_existing_key_named_folder_is_not_migrated(self):
        folder = self.vault / "PAPER001"
        folder.mkdir()
        (folder / "meta.md").write_text(render_meta(item()["data"]), encoding="utf-8")
        summary = self.sync()
        self.assertEqual(summary.skipped, 1)
        self.assertTrue(folder.is_dir())
        self.assertFalse(item_folder(self.vault).exists())

    def test_duplicate_partial_sidecars_are_a_conflict(self):
        for name in ("first [PAPER001]", "second [PAPER001]"):
            folder = self.vault / name
            folder.mkdir()
            sidecar = folder / "temp" / "zotero-item.json"
            sidecar.parent.mkdir()
            sidecar.write_text(
                render_zotero_json(item()["data"]), encoding="utf-8"
            )
        summary = self.sync()
        self.assertEqual(summary.conflicts, 1)
        self.assertEqual(summary.created, 0)
        self.assertFalse(any(self.vault.glob("*/meta.md")))

    def test_removed_or_changed_zotero_item_has_no_local_effect(self):
        self.sync()
        path = item_folder(self.vault) / "meta.md"
        original = path.read_bytes()
        self.sync([])
        self.sync([item(title="changed")])
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_item_does_not_block_another_item(self):
        bad = item("PAPER001")
        bad["data"]["key"] = "MISMATCH"
        summary = self.sync([bad, item("PAPER002")])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.created, 1)
        self.assertTrue((item_folder(self.vault, item("PAPER002")) / "meta.md").is_file())

    def test_invalid_title_type_does_not_block_another_item(self):
        bad = item("PAPER001")
        bad["data"]["shortTitle"] = 42
        good = item("PAPER002")
        summary = self.sync([bad, good])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.created, 1)
        self.assertTrue((item_folder(self.vault, good) / "meta.md").is_file())

    def test_duplicate_api_key_is_a_conflict_and_not_published(self):
        summary = self.sync([item(), item(title="other")])
        self.assertEqual(summary.conflicts, 1)
        self.assertFalse(item_folder(self.vault).exists())

    def test_duplicate_local_key_is_reported_without_overwrite(self):
        for folder in ("first", "second"):
            path = self.vault / folder / "meta.md"
            path.parent.mkdir()
            path.write_text(render_meta(item()["data"]), encoding="utf-8")
        summary = self.sync()
        self.assertEqual(summary.conflicts, 1)
        self.assertFalse(item_folder(self.vault).exists())

    def test_malformed_existing_meta_aborts_before_writes(self):
        bad = self.vault / "bad" / "meta.md"
        bad.parent.mkdir()
        bad.write_text("---\nnot: [closed\n", encoding="utf-8")
        with self.assertRaises(PipelineError):
            self.sync([item("PAPER002")])
        self.assertFalse(item_folder(self.vault, item("PAPER002")).exists())

    def test_existing_folder_contents_are_preserved(self):
        folder = item_folder(self.vault)
        folder.mkdir()
        marker = folder / "my-note.txt"
        marker.write_text("keep", encoding="utf-8")
        summary = self.sync()
        self.assertEqual(summary.created, 1)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertTrue((folder / "meta.md").is_file())
        self.assertTrue((folder / "temp" / "zotero-item.json").is_file())

    def test_nested_meta_is_not_indexed(self):
        nested = self.vault / "group" / "PAPER001" / "meta.md"
        nested.parent.mkdir(parents=True)
        nested.write_text(render_meta(item()["data"]), encoding="utf-8")
        summary = self.sync()
        self.assertEqual(summary.created, 1)
        self.assertTrue((item_folder(self.vault) / "meta.md").is_file())

    def test_non_paper_directories_are_untouched(self):
        obsidian = self.vault / ".obsidian"
        obsidian.mkdir()
        settings = obsidian / "app.json"
        settings.write_text("{}", encoding="utf-8")
        self.sync()
        self.assertEqual(settings.read_text(encoding="utf-8"), "{}")

    def test_attachment_note_and_annotation_are_skipped(self):
        entries = []
        for number, item_type in enumerate(("attachment", "note", "annotation"), start=1):
            entry = item(f"SKIP000{number}")
            entry["data"]["itemType"] = item_type
            entries.append(entry)
        summary = self.sync(entries)
        self.assertEqual(summary.skipped, 3)
        self.assertEqual(list(self.vault.iterdir()), [])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_symlink_paper_directory_is_never_followed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        marker = outside / "temp" / "zotero-item.json"
        marker.parent.mkdir()
        marker.write_text(render_zotero_json(item()["data"]), encoding="utf-8")
        original = marker.read_bytes()
        item_folder(self.vault).symlink_to(outside, target_is_directory=True)
        summary = self.sync()
        self.assertEqual(summary.failed, 1)
        self.assertFalse((outside / "meta.md").exists())
        self.assertEqual(marker.read_bytes(), original)

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_symlink_temp_directory_is_never_followed(self):
        folder = item_folder(self.vault)
        folder.mkdir()
        outside = Path(self.temp.name) / "outside-temp"
        outside.mkdir()
        marker = outside / "zotero-item.json"
        marker.write_text(render_zotero_json(item()["data"]), encoding="utf-8")
        (folder / "temp").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(PipelineError):
            self.sync()

        self.assertFalse((folder / "meta.md").exists())
        self.assertEqual(read_zotero_json(marker), item()["data"])


@unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
class PdfLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = Config("COLLECT1", "http://localhost:23119/api/")

    def source(self, name: str, content: bytes = b"%PDF-test") -> Path:
        path = self.root / "zotero" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return path

    def test_sync_links_all_pdfs_without_copying(self):
        first = self.source("main.pdf", b"%PDF-main")
        second = self.source("supp.pdf", b"%PDF-supplement")
        children = [
            pdf_attachment("PDFKEY01", filename="Main: Paper.pdf"),
            pdf_attachment("PDFKEY02", filename="Supplement.pdf"),
        ]
        zotero = FakeZotero(
            [item()],
            {"PAPER001": children},
            {"PDFKEY01": first.as_uri(), "PDFKEY02": second.as_uri()},
        )
        summary = Importer(self.vault, self.config, zotero).sync()
        folder = item_folder(self.vault)
        main = folder / "Main - Paper [PDFKEY01].pdf"
        supplement = folder / "Supplement [PDFKEY02].pdf"
        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.pdf_linked, 2)
        self.assertTrue(main.is_symlink())
        self.assertTrue(supplement.is_symlink())
        self.assertEqual(main.resolve(), first.resolve())
        self.assertEqual(supplement.resolve(), second.resolve())
        self.assertEqual(first.read_bytes(), b"%PDF-main")

    def test_missing_pdf_does_not_block_metadata(self):
        summary = Importer(self.vault, self.config, FakeZotero([item()])).sync()
        folder = item_folder(self.vault)
        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.pdf_missing, 1)
        self.assertEqual(summary.pdf_failed, 0)
        self.assertTrue((folder / "meta.md").is_file())
        self.assertTrue((folder / "temp" / "zotero-item.json").is_file())

    def test_sync_links_new_papers_with_at_most_four_concurrent_sessions(self):
        entries = [item(f"PAPER{number:03d}") for number in range(1, 7)]
        lock = threading.Lock()
        release = threading.Event()
        shared = {"active": 0, "peak": 0, "sessions": []}

        class ConcurrentZotero(FakeZotero):
            def new_session(session_self):
                session = ConcurrentZotero(
                    session_self.entries,
                    session_self.children_by_parent,
                    session_self.file_urls,
                )
                with lock:
                    shared["sessions"].append(session)
                return session

            def children(session_self, parent_key):
                del session_self, parent_key
                with lock:
                    shared["active"] += 1
                    shared["peak"] = max(shared["peak"], shared["active"])
                    if shared["peak"] == 4:
                        release.set()
                if not release.wait(timeout=2):
                    raise AssertionError("four PDF link workers did not run concurrently")
                with lock:
                    shared["active"] -= 1
                return []

        progress = []
        summary = Importer(
            self.vault,
            self.config,
            ConcurrentZotero(entries),
        ).sync(progress.append)

        self.assertEqual(summary.created, 6)
        self.assertEqual(summary.pdf_missing, 6)
        self.assertEqual(shared["peak"], 4)
        self.assertEqual(len(shared["sessions"]), 6)
        self.assertEqual(len({id(session) for session in shared["sessions"]}), 6)
        self.assertIn("PDF 链接进度：6/6", progress)

    def test_sync_merges_concurrent_pdf_messages_in_item_order(self):
        entries = [item(f"PAPER{number:03d}") for number in range(1, 4)]

        class ReverseCompletionImporter(Importer):
            def _link_paper(self, item_key, folder):
                del folder
                time.sleep({"PAPER001": 0.03, "PAPER002": 0.02, "PAPER003": 0.01}[item_key])
                return PdfLinkSummary(papers=1, failed=1, messages=[item_key])

        summary = ReverseCompletionImporter(
            self.vault,
            self.config,
            FakeZotero(entries),
        ).sync()

        self.assertEqual(summary.messages, ["PAPER001", "PAPER002", "PAPER003"])
        self.assertEqual(summary.pdf_failed, 3)

    def test_repeat_sync_does_not_create_pdf_sessions(self):
        class TrackingZotero(FakeZotero):
            def __init__(self, *args, session_count=None, **kwargs):
                super().__init__(*args, **kwargs)
                self.session_count = session_count if session_count is not None else []

            def new_session(self):
                self.session_count.append(1)
                return TrackingZotero(
                    self.entries,
                    self.children_by_parent,
                    self.file_urls,
                    session_count=self.session_count,
                )

        zotero = TrackingZotero([item()])
        importer = Importer(self.vault, self.config, zotero)
        importer.sync()
        self.assertEqual(len(zotero.session_count), 1)

        importer.sync()
        self.assertEqual(len(zotero.session_count), 1)

    def test_unavailable_pdf_reports_failure_after_metadata_success(self):
        missing = (self.root / "missing.pdf").as_uri()
        zotero = FakeZotero(
            [item()],
            {"PAPER001": [pdf_attachment("PDFKEY01")]},
            {"PDFKEY01": missing},
        )
        summary = Importer(self.vault, self.config, zotero).sync()
        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.pdf_failed, 1)
        self.assertFalse(summary.successful)
        self.assertTrue((item_folder(self.vault) / "meta.md").is_file())

    def test_link_existing_repairs_target_and_filename(self):
        folder = self.vault / "renamed paper"
        folder.mkdir()
        (folder / "meta.md").write_text(render_meta(item()["data"]), encoding="utf-8")
        old = self.source("old.pdf")
        new = self.source("new.pdf")
        old_link = folder / "Old name [PDFKEY01].pdf"
        old_link.symlink_to(old)
        zotero = FakeZotero(
            [],
            {"PAPER001": [pdf_attachment("PDFKEY01", filename="New name.pdf")]},
            {"PDFKEY01": new.as_uri()},
        )
        summary = PdfLinker(zotero).link_existing(self.vault)
        repaired = folder / "New name [PDFKEY01].pdf"
        self.assertEqual(summary.papers, 1)
        self.assertEqual(summary.linked, 1)
        self.assertFalse(old_link.exists())
        self.assertTrue(repaired.is_symlink())
        self.assertEqual(repaired.resolve(), new.resolve())

        repeated = PdfLinker(zotero).link_existing(self.vault)
        self.assertEqual(repeated.unchanged, 1)
        self.assertEqual(repeated.linked, 0)

        new.unlink()
        replacement = self.source("replacement.pdf")
        zotero.file_urls["PDFKEY01"] = replacement.as_uri()
        repaired_again = PdfLinker(zotero).link_existing(self.vault)
        self.assertEqual(repaired_again.linked, 1)
        self.assertEqual(repaired.resolve(), replacement.resolve())

    def test_equivalent_paths_to_same_pdf_are_accepted(self):
        source = self.source("source.pdf")
        alias = self.root / "source-alias.pdf"
        alias.symlink_to(source)
        folder = self.vault / "paper"
        folder.mkdir()
        managed = folder / "paper [PDFKEY01].pdf"
        managed.symlink_to(alias)
        zotero = FakeZotero(
            [],
            {"PAPER001": [pdf_attachment("PDFKEY01")]},
            {"PDFKEY01": source.as_uri()},
        )
        linker = PdfLinker(zotero)

        linked = linker.link_paper(folder, "PAPER001")
        self.assertEqual(linked.unchanged, 1)
        self.assertEqual(linked.linked, 0)

    def test_ordinary_file_collision_is_preserved(self):
        folder = self.vault / "paper"
        folder.mkdir()
        (folder / "meta.md").write_text(render_meta(item()["data"]), encoding="utf-8")
        collision = folder / "paper [PDFKEY01].pdf"
        collision.write_bytes(b"mine")
        source = self.source("source.pdf")
        zotero = FakeZotero(
            [],
            {"PAPER001": [pdf_attachment("PDFKEY01")]},
            {"PDFKEY01": source.as_uri()},
        )
        summary = PdfLinker(zotero).link_existing(self.vault)
        self.assertEqual(summary.conflicts, 1)
        self.assertEqual(collision.read_bytes(), b"mine")

    def test_uri_and_link_name_validation(self):
        source = self.source("论文 文件.pdf")
        self.assertEqual(file_uri_path(source.as_uri()), source.resolve())
        self.assertEqual(pdf_link_name("A/B?.PDF", "PDFKEY01"), "A - B [PDFKEY01].pdf")
        with self.assertRaises(PipelineError):
            file_uri_path("https://example.com/paper.pdf")

    def test_non_pdf_children_are_ignored(self):
        child = pdf_attachment("PDFKEY01", filename="snapshot.html")
        child["data"]["contentType"] = "text/html"
        zotero = FakeZotero([], {"PAPER001": [child]})
        folder = self.vault / "paper"
        folder.mkdir()
        summary = PdfLinker(zotero).link_paper(folder, "PAPER001")
        self.assertEqual(summary.missing, 1)
        self.assertEqual(summary.failed, 0)


class ConfigAndFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "existing vault"
        self.vault.mkdir()

    def test_init_preserves_existing_vault_and_writes_minimal_config(self):
        existing = self.vault / "Home.md"
        existing.write_text("mine", encoding="utf-8")
        initialize(self.vault, "COLLECT1", "http://localhost:23119/api/")
        self.assertEqual(existing.read_text(encoding="utf-8"), "mine")
        self.assertEqual(load_config(self.vault), Config("COLLECT1", "http://localhost:23119/api/"))
        self.assertEqual(
            set(load_config(self.vault).__dict__), {"collection", "zotero_url", "pdf2md"}
        )
        self.assertEqual(load_config(self.vault).pdf2md, Pdf2MdConfig())
        self.assertFalse((self.vault / "papers").exists())

    def test_init_never_overwrites_config(self):
        initialize(self.vault, "COLLECT1", "http://localhost:23119/api/")
        original = (self.vault / ".papermarrow" / "config.toml").read_bytes()
        with self.assertRaises(PipelineError):
            initialize(self.vault, "COLLECT2", "http://localhost:23119/api/")
        self.assertEqual((self.vault / ".papermarrow" / "config.toml").read_bytes(), original)

    def test_atomic_create_never_overwrites(self):
        path = self.vault / "meta.md"
        atomic_create(path, "first")
        with self.assertRaises(FileExistsError):
            atomic_create(path, "second")
        self.assertEqual(path.read_text(encoding="utf-8"), "first")

    def test_writer_lock_rejects_second_process(self):
        with VaultLock(self.vault):
            with self.assertRaises(PipelineError):
                with VaultLock(self.vault):
                    pass
        with VaultLock(self.vault):
            pass

    def test_index_only_reads_direct_children(self):
        direct = self.vault / "renamed" / "meta.md"
        direct.parent.mkdir()
        direct.write_text(render_meta(item()["data"]), encoding="utf-8")
        nested = self.vault / "group" / "nested" / "meta.md"
        nested.parent.mkdir(parents=True)
        nested.write_text(render_meta(item("PAPER002")["data"]), encoding="utf-8")
        self.assertEqual(index_existing(self.vault).keys, {"PAPER001"})

    def test_key_and_url_validation(self):
        self.assertEqual(key("ABCD1234"), "ABCD1234")
        self.assertEqual(validate_url("http://127.0.0.1:23119/api/"), "http://127.0.0.1:23119/api/")
        for value in ("bad", "../../xx", "abcd1234"):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                key(value)
        for value in ("https://localhost/api/", "http://example.com/api/", "http://localhost/api/?token=x"):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                validate_url(value)

    def test_pdf2md_config_is_written_and_validated(self):
        initialize(
            self.vault,
            "COLLECT1",
            "http://localhost:23119/api/",
            pdf2md_model="pipeline",
            pdf2md_language="ch",
            pdf2md_ocr=True,
            pdf2md_timeout=3600,
        )
        self.assertEqual(
            load_config(self.vault).pdf2md,
            Pdf2MdConfig("pipeline", "ch", True, 3600),
        )

    def test_deprecated_enabled_is_accepted_but_ignored(self):
        config = self.vault / ".papermarrow" / "config.toml"
        config.parent.mkdir()
        config.write_text(
            'collection = "COLLECT1"\n'
            'zotero_url = "http://localhost:23119/api/"\n'
            "[pdf2md]\n"
            "enabled = true\n",
            encoding="utf-8",
        )
        self.assertEqual(load_config(self.vault).pdf2md, Pdf2MdConfig())

    def test_pdf2md_config_rejects_unknown_or_invalid_values(self):
        config = self.vault / ".papermarrow" / "config.toml"
        config.parent.mkdir()
        base = 'collection = "COLLECT1"\nzotero_url = "http://localhost:23119/api/"\n'
        for table in (
            "[pdf2md]\nenabled = true\nunknown = 1\n",
            "[pdf2md]\nenabled = \"yes\"\n",
            "[pdf2md]\ntimeout = 0\n",
        ):
            with self.subTest(table=table):
                config.write_text(base + table, encoding="utf-8")
                with self.assertRaises(PipelineError):
                    load_config(self.vault)


class Pdf2MdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-input")

    def options(self, output_root: Path | None = None) -> ConversionOptions:
        return ConversionOptions(
            pdf=self.pdf,
            output_root=output_root or self.root / "results",
            token="secret",
            model="pipeline",
            language="ch",
            ocr=True,
            timeout=45,
        )

    def test_organize_removes_returned_pdf_and_requires_full_markdown(self):
        result = self.root / "result"
        result.mkdir()
        FakeMinerUResult().save_all(str(result))
        organize_result(result)
        self.assertTrue((result / "full.md").is_file())
        self.assertFalse((result / "paper_origin.pdf").exists())
        self.assertTrue((result / "images" / "figure.png").is_file())
        self.assertTrue((result / "temp" / "content_list.md").is_file())
        self.assertTrue((result / "temp" / "layout.json").is_file())

        missing = self.root / "missing"
        missing.mkdir()
        FakeMinerUResult(include_full=False).save_all(str(missing))
        with self.assertRaises(ConversionError):
            organize_result(missing)

    def test_staging_mode_inherits_windows_acl_but_remains_private_on_posix(self):
        expected = 0o777 if os.name == "nt" else 0o700
        self.assertEqual(_staging_mode(), expected)

    def test_standalone_conversion_keeps_result_directory_but_no_pdf_copy(self):
        client = FakeMinerUClient("secret")
        destination = convert_pdf(self.options(), client_factory=lambda token: client)
        self.assertEqual(destination, self.root / "results" / "source")
        self.assertTrue((destination / "full.md").is_file())
        self.assertFalse(any(destination.glob("*.pdf")))
        self.assertEqual(client.token, "secret")
        self.assertEqual(client.calls[0][1]["language"], "ch")
        self.assertTrue(client.calls[0][1]["ocr"])

    def test_pipeline_conversion_publishes_directly_and_preserves_input(self):
        paper = self.root / "paper"
        paper.mkdir()
        (paper / "meta.md").write_text("metadata", encoding="utf-8")
        existing_temp = paper / "temp"
        existing_temp.mkdir()
        sidecar = existing_temp / "zotero-item.json"
        sidecar.write_text('{"key":"PAPER001"}\n', encoding="utf-8")
        marker = existing_temp / "mine.txt"
        marker.write_text("keep", encoding="utf-8")
        convert_pdf_into_paper(
            self.options(paper),
            paper,
            client_factory=lambda token: FakeMinerUClient(token),
        )
        self.assertEqual((paper / "full.md").read_text(encoding="utf-8"), "# Converted\n")
        self.assertTrue((paper / "images" / "figure.png").is_file())
        self.assertTrue((paper / "temp" / "content_list.md").is_file())
        self.assertTrue((paper / "temp" / "layout.json").is_file())
        self.assertEqual(sidecar.read_text(encoding="utf-8"), '{"key":"PAPER001"}\n')
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertFalse(any(paper.glob("*_origin.pdf")))
        self.assertEqual(self.pdf.read_bytes(), b"%PDF-input")
        self.assertEqual((paper / "meta.md").read_text(encoding="utf-8"), "metadata")

    def test_pipeline_conversion_conflict_publishes_nothing(self):
        paper = self.root / "paper"
        paper.mkdir()
        (paper / "full.md").write_text("mine", encoding="utf-8")
        factory_calls = []

        def factory(token):
            factory_calls.append(token)
            return FakeMinerUClient(token)

        with self.assertRaises(ConversionError):
            convert_pdf_into_paper(
                self.options(paper),
                paper,
                client_factory=factory,
            )
        self.assertEqual(factory_calls, [])
        self.assertEqual((paper / "full.md").read_text(encoding="utf-8"), "mine")
        self.assertFalse((paper / "images").exists())
        self.assertFalse((paper / "temp").exists())

    def test_pipeline_conversion_never_overwrites_existing_temp_entry(self):
        paper = self.root / "paper"
        temp = paper / "temp"
        temp.mkdir(parents=True)
        existing = temp / "layout.json"
        existing.write_text("mine", encoding="utf-8")
        client = FakeMinerUClient("secret")

        with self.assertRaises(ConversionError):
            convert_pdf_into_paper(
                self.options(paper),
                paper,
                client_factory=lambda token: client,
            )

        self.assertEqual(existing.read_text(encoding="utf-8"), "mine")
        self.assertFalse((paper / "full.md").exists())
        self.assertFalse((paper / "images").exists())
        self.assertEqual(len(client.calls), 1)

    def test_pipeline_conversion_rolls_back_an_ordinary_publish_error(self):
        paper = self.root / "paper"
        paper.mkdir()
        temp = paper / "temp"
        temp.mkdir()
        marker = temp / "zotero-item.json"
        marker.write_text("metadata", encoding="utf-8")
        with (
            patch("paper_marrow.pdf2md.os.link", side_effect=OSError("disk error")),
            self.assertRaises(ConversionError),
        ):
            convert_pdf_into_paper(
                self.options(paper),
                paper,
                client_factory=lambda token: FakeMinerUClient(token),
            )
        self.assertFalse((paper / "full.md").exists())
        self.assertFalse((paper / "images").exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "metadata")
        self.assertEqual(list(temp.iterdir()), [marker])

    def test_vault_token_prefers_environment_then_vault_env_file(self):
        vault = self.root / "vault"
        env_file = vault / ".papermarrow" / ".env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("MINERU_TOKEN=file-token\n", encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_vault_token(vault), "file-token")
        with patch.dict("os.environ", {"MINERU_TOKEN": "real-token"}, clear=True):
            self.assertEqual(load_vault_token(vault), "real-token")
        with patch.dict("os.environ", {}, clear=True):
            env_file.unlink()
            with self.assertRaises(ConversionError):
                load_vault_token(vault)


class ConversionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = Config("COLLECT1", "http://localhost:23119/api/")

    def source(self, name: str = "source.pdf") -> Path:
        path = self.root / name
        path.write_bytes(b"%PDF")
        return path

    def test_historical_ready_paper_can_be_selected_explicitly(self):
        source = self.source()
        zotero = FakeZotero(
            [item()],
            {"PAPER001": [pdf_attachment("PDFKEY01")]},
            {"PDFKEY01": source.as_uri()},
        )
        Importer(self.vault, self.config, zotero).sync()
        (item_folder(self.vault) / "temp" / "user-note.txt").write_text(
            "keep", encoding="utf-8"
        )
        service = ConversionService(self.vault, self.config)
        self.assertEqual(service.list_candidates()[0].status, "Ready")
        observed = []

        def convert(_options, folder):
            with VaultLock(self.vault):
                observed.append("main lock released")
            (folder / "full.md").write_text("# converted\n", encoding="utf-8")

        with patch("paper_marrow.conversion.convert_pdf_into_paper", side_effect=convert):
            summary = service.convert_selected(["PAPER001"], "token")
        self.assertEqual(summary.converted, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(observed, ["main lock released"])

        with patch("paper_marrow.conversion.convert_pdf_into_paper") as converter:
            repeated = service.convert_selected(["PAPER001"], "token")
        self.assertEqual(repeated.skipped, 1)
        converter.assert_not_called()

    def test_list_reports_unavailable_converted_and_ready(self):
        entries = [item(f"PAPER00{number}") for number in range(1, 5)]
        first, second = self.source("first.pdf"), self.source("second.pdf")
        ready = self.source("ready.pdf")
        converted = self.source("converted.pdf")
        zotero = FakeZotero(
            entries,
            {
                "PAPER001": [],
                "PAPER002": [
                    pdf_attachment("PDFKEY01", "PAPER002"),
                    pdf_attachment("PDFKEY02", "PAPER002"),
                ],
                "PAPER003": [pdf_attachment("PDFKEY03", "PAPER003")],
                "PAPER004": [pdf_attachment("PDFKEY04", "PAPER004")],
            },
            {
                "PDFKEY01": first.as_uri(),
                "PDFKEY02": second.as_uri(),
                "PDFKEY03": converted.as_uri(),
                "PDFKEY04": ready.as_uri(),
            },
        )
        Importer(self.vault, self.config, zotero).sync()
        (item_folder(self.vault, entries[2]) / "full.md").write_text(
            "converted", encoding="utf-8"
        )
        statuses = {
            candidate.item_key: candidate.status
            for candidate in ConversionService(self.vault, self.config).list_candidates()
        }
        self.assertEqual(
            statuses,
            {
                "PAPER001": "Unavailable",
                "PAPER002": "Unavailable",
                "PAPER003": "Converted",
                "PAPER004": "Ready",
            },
        )

    def test_multiple_pdf_never_calls_converter(self):
        first, second = self.source("first.pdf"), self.source("second.pdf")
        multiple_zotero = FakeZotero(
            [item()],
            {"PAPER001": [pdf_attachment("PDFKEY01"), pdf_attachment("PDFKEY02")]},
            {"PDFKEY01": first.as_uri(), "PDFKEY02": second.as_uri()},
        )
        Importer(self.vault, self.config, multiple_zotero).sync()
        service = ConversionService(self.vault, self.config)
        with patch("paper_marrow.conversion.convert_pdf_into_paper") as converter:
            summary = service.convert_selected(["PAPER001"], "token")
        self.assertEqual(summary.failed, 1)
        converter.assert_not_called()

    def test_unavailable_pdf_is_listed_without_calling_converter(self):
        missing = self.root / "missing.pdf"
        zotero = FakeZotero(
            [item()],
            {"PAPER001": [pdf_attachment("PDFKEY01")]},
            {"PDFKEY01": missing.as_uri()},
        )
        Importer(self.vault, self.config, zotero).sync()
        service = ConversionService(self.vault, self.config)
        self.assertEqual(service.list_candidates()[0].status, "Unavailable")
        with patch("paper_marrow.conversion.convert_pdf_into_paper") as converter:
            summary = service.convert_selected(["PAPER001"], "token")
        self.assertEqual(summary.failed, 1)
        converter.assert_not_called()

    def test_only_one_valid_managed_pdf_symlink_is_ready(self):
        service = ConversionService(self.vault, self.config)
        source = self.source()

        ordinary = self.vault / "ordinary"
        ordinary.mkdir()
        (ordinary / "Paper [PDFKEY01].pdf").write_bytes(b"%PDF")
        candidate = service.inspect("PAPER001", ordinary)
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("没有受管理", candidate.detail)

        unmanaged = self.vault / "unmanaged"
        unmanaged.mkdir()
        (unmanaged / "paper.pdf").symlink_to(source)
        candidate = service.inspect("PAPER001", unmanaged)
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("没有受管理", candidate.detail)

        broken = self.vault / "broken"
        broken.mkdir()
        (broken / "Paper [PDFKEY01].pdf").symlink_to(self.root / "absent.pdf")
        candidate = service.inspect("PAPER001", broken)
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("已经失效", candidate.detail)

        directory_target = self.vault / "directory-target"
        directory_target.mkdir()
        target_directory = self.root / "not-a-file"
        target_directory.mkdir()
        (directory_target / "Paper [PDFKEY01].pdf").symlink_to(
            target_directory, target_is_directory=True
        )
        candidate = service.inspect("PAPER001", directory_target)
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("不是普通文件", candidate.detail)

        multiple = self.vault / "multiple"
        multiple.mkdir()
        (multiple / "First [PDFKEY01].pdf").symlink_to(source)
        (multiple / "Second [PDFKEY02].pdf").symlink_to(source)
        candidate = service.inspect("PAPER001", multiple)
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("有 2 个", candidate.detail)

        ready = self.vault / "ready"
        ready.mkdir()
        link = ready / "Paper [PDFKEY01].pdf"
        link.symlink_to(source)
        candidate = service.inspect("PAPER001", ready)
        self.assertEqual(candidate.status, "Ready")
        self.assertEqual(candidate.pdf, link)

    def test_selected_paper_is_rechecked_after_listing(self):
        source = self.source()
        folder = self.vault / "paper"
        folder.mkdir()
        (folder / "meta.md").write_text(render_meta(item()["data"]), encoding="utf-8")
        link = folder / "Paper [PDFKEY01].pdf"
        link.symlink_to(source)
        service = ConversionService(self.vault, self.config)
        self.assertEqual(service.list_candidates()[0].status, "Ready")

        link.unlink()
        with patch("paper_marrow.conversion.convert_pdf_into_paper") as converter:
            summary = service.convert_selected(["PAPER001"], "token")
        self.assertEqual(summary.failed, 1)
        self.assertIn("没有受管理", summary.messages[0])
        converter.assert_not_called()

    def test_output_conflicts_are_unavailable(self):
        service = ConversionService(self.vault, self.config)

        invalid_full = self.vault / "invalid-full"
        invalid_full.mkdir()
        (invalid_full / "full.md").symlink_to(self.root / "absent.md")
        candidate = service.inspect("PAPER001", invalid_full)
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("full.md 不是普通文件", candidate.detail)

        occupied_output = self.vault / "occupied-output"
        occupied_output.mkdir()
        (occupied_output / "images").mkdir()
        candidate = service.inspect("PAPER001", occupied_output)
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("images", candidate.detail)

        invalid_temp = self.vault / "invalid-temp"
        invalid_temp.mkdir()
        (invalid_temp / "temp").write_text("not a directory", encoding="utf-8")
        candidate = service.inspect("PAPER001", invalid_temp)
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("temp 不是普通目录", candidate.detail)

    def test_duplicate_local_key_is_unavailable(self):
        for name in ("first", "second"):
            folder = self.vault / name
            folder.mkdir()
            (folder / "meta.md").write_text(render_meta(item()["data"]), encoding="utf-8")
        candidate = ConversionService(self.vault, self.config).list_candidates()[0]
        self.assertEqual(candidate.status, "Unavailable")
        self.assertIn("多个论文目录", candidate.detail)

    def test_selected_conversion_failure_does_not_block_later_key(self):
        entries = [item("PAPER001"), item("PAPER002")]
        first, second = self.source("first.pdf"), self.source("second.pdf")
        zotero = FakeZotero(
            entries,
            {
                "PAPER001": [pdf_attachment("PDFKEY01", "PAPER001")],
                "PAPER002": [pdf_attachment("PDFKEY02", "PAPER002")],
            },
            {"PDFKEY01": first.as_uri(), "PDFKEY02": second.as_uri()},
        )
        Importer(self.vault, self.config, zotero).sync()
        calls = []

        def convert(_options, folder):
            calls.append(folder.name)
            if len(calls) == 1:
                raise ConversionError("first failed")

        service = ConversionService(self.vault, self.config)
        with patch("paper_marrow.conversion.convert_pdf_into_paper", side_effect=convert):
            summary = service.convert_selected(["PAPER001", "PAPER002"], "token")
        self.assertEqual(summary.converted, 1)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(len(calls), 2)


class AdapterAndCliTests(unittest.TestCase):
    def test_vault_is_discovered_from_a_deep_paper_subdirectory(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            working_directory = vault / "paper" / "images"
            working_directory.mkdir(parents=True)
            with patch("paper_marrow.cli.Path.cwd", return_value=working_directory):
                self.assertEqual(_vault(None), vault.resolve())

    def test_vault_discovery_uses_nearest_marker_and_explicit_value_wins(self):
        with tempfile.TemporaryDirectory() as root:
            outer = Path(root) / "outer"
            inner = outer / "paper" / "nested-vault"
            explicit = Path(root) / "explicit"
            initialize(outer, "COLLECT1", "http://localhost:23119/api/")
            initialize(inner, "COLLECT2", "http://localhost:23119/api/")
            working_directory = inner / "paper" / "temp"
            working_directory.mkdir(parents=True)
            with patch("paper_marrow.cli.Path.cwd", return_value=working_directory):
                self.assertEqual(_vault(None), inner.resolve())
                self.assertEqual(_vault(explicit), explicit.resolve())

    def test_init_without_vault_uses_current_directory_not_parent_vault(self):
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root) / "parent-vault"
            initialize(parent, "COLLECT1", "http://localhost:23119/api/")
            current = parent / "new-vault"
            current.mkdir()
            with (
                patch("paper_marrow.cli.Path.cwd", return_value=current),
                redirect_stdout(io.StringIO()),
            ):
                result = main(["init", "--collection", "COLLECT2"])
            self.assertEqual(result, 0)
            self.assertEqual(load_config(current).collection, "COLLECT2")

    def test_cli_sync_discovers_vault_without_argument(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            working_directory = vault / "existing paper" / "images"
            working_directory.mkdir(parents=True)
            with (
                patch("paper_marrow.cli.Path.cwd", return_value=working_directory),
                patch("paper_marrow.cli.Zotero", return_value=FakeZotero()),
                redirect_stdout(io.StringIO()),
            ):
                result = main(["sync"])
            self.assertEqual(result, 0)
            self.assertTrue((item_folder(vault) / "meta.md").is_file())

    def test_missing_vault_marker_fails_before_zotero_access(self):
        with tempfile.TemporaryDirectory() as root:
            output = io.StringIO()
            with (
                patch("paper_marrow.cli.Path.cwd", return_value=Path(root)),
                patch(
                    "paper_marrow.cli.Zotero",
                    side_effect=AssertionError("Zotero must not be accessed"),
                ),
                redirect_stderr(output),
            ):
                result = main(["sync"])
            self.assertEqual(result, 1)
            self.assertIn(".papermarrow/config.toml", output.getvalue())

    def test_invalid_nearest_marker_does_not_fall_back_to_outer_vault(self):
        with tempfile.TemporaryDirectory() as root:
            outer = Path(root) / "outer"
            initialize(outer, "COLLECT1", "http://localhost:23119/api/")
            inner = outer / "paper"
            marker = inner / ".papermarrow" / "config.toml"
            marker.parent.mkdir(parents=True)
            marker.write_text("invalid toml =", encoding="utf-8")
            working_directory = inner / "images"
            working_directory.mkdir()
            output = io.StringIO()
            with (
                patch("paper_marrow.cli.Path.cwd", return_value=working_directory),
                patch(
                    "paper_marrow.cli.Zotero",
                    side_effect=AssertionError("Zotero must not be accessed"),
                ),
                redirect_stderr(output),
            ):
                result = main(["sync"])
            self.assertEqual(result, 1)
            self.assertIn(str(marker), output.getvalue())

    def test_list_paginates_and_rejects_inconsistent_snapshot(self):
        api = Zotero()

        def request(path):
            api.last_version = "1" if "start=0" in path else "2"
            return [{}] * 100 if "start=0" in path else []

        with patch.object(api, "request", side_effect=request):
            with self.assertRaises(PipelineError):
                api.listing("users/0/collections")

    def test_zotero_new_session_is_independent_and_pinned(self):
        api = Zotero()
        api.server_id = "database-id"
        session = api.new_session()

        self.assertIsNot(session, api)
        self.assertIsNot(session.opener, api.opener)
        self.assertEqual(session.url, api.url)
        self.assertEqual(session.server_id, "database-id")
        self.assertEqual(session.last_version, "")

    def test_cli_sync_reports_summary_and_exit_code(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            output = io.StringIO()
            progress = io.StringIO()
            with (
                patch("paper_marrow.cli.Zotero", return_value=FakeZotero()),
                redirect_stdout(output),
                redirect_stderr(progress),
            ):
                result = main(["sync", "--vault", str(vault)])
            self.assertEqual(result, 0)
            self.assertIn("created=1 skipped=0 conflicts=0 failed=0", output.getvalue())
            self.assertIn("pdf_linked=0 pdf_missing=1 pdf_failed=0", output.getvalue())
            self.assertIn("本轮新增 key：PAPER001", output.getvalue())
            self.assertIn("正在读取 Zotero Collection", progress.getvalue())
            self.assertIn("PDF 链接进度：1/1", progress.getvalue())

    def test_cli_sync_never_requires_mineru_token(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            with (
                patch.dict("os.environ", {}, clear=True),
                patch("paper_marrow.cli.Zotero", return_value=FakeZotero()),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(["sync", "--vault", str(vault)]), 0)
            self.assertEqual(len(list(vault.glob("*/meta.md"))), 1)

    def test_cli_convert_requires_token_without_constructing_zotero(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            with (
                patch.dict("os.environ", {}, clear=True),
                patch(
                    "paper_marrow.cli.Zotero",
                    side_effect=AssertionError("Zotero must not be constructed"),
                ),
                redirect_stderr(io.StringIO()),
            ):
                result = main(["convert", "--vault", str(vault), "--key", "PAPER001"])
            self.assertEqual(result, 1)

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_cli_convert_selected_releases_main_lock(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            vault = root_path / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            source = root_path / "source.pdf"
            source.write_bytes(b"%PDF")
            fake_zotero = FakeZotero(
                [item()],
                {"PAPER001": [pdf_attachment("PDFKEY01")]},
                {"PDFKEY01": source.as_uri()},
            )
            Importer(vault, load_config(vault), fake_zotero).sync()
            observed = []

            def convert(_options, _folder):
                with VaultLock(vault):
                    observed.append("main lock released")

            output = io.StringIO()
            with (
                patch.dict("os.environ", {"MINERU_TOKEN": "token"}, clear=True),
                patch(
                    "paper_marrow.cli.Zotero",
                    side_effect=AssertionError("Zotero must not be constructed"),
                ),
                patch("paper_marrow.conversion.convert_pdf_into_paper", side_effect=convert),
                redirect_stdout(output),
            ):
                result = main(["convert", "--vault", str(vault), "--key", "PAPER001"])
            self.assertEqual(result, 0)
            self.assertEqual(observed, ["main lock released"])
            self.assertIn("selected=1 converted=1 skipped=0 failed=0", output.getvalue())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_cli_convert_list_needs_no_token(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            vault = root_path / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            source = root_path / "source.pdf"
            source.write_bytes(b"%PDF")
            fake = FakeZotero(
                [item()],
                {"PAPER001": [pdf_attachment("PDFKEY01")]},
                {"PDFKEY01": source.as_uri()},
            )
            Importer(vault, load_config(vault), fake).sync()
            output = io.StringIO()
            with (
                patch.dict("os.environ", {}, clear=True),
                patch(
                    "paper_marrow.cli.Zotero",
                    side_effect=AssertionError("Zotero must not be constructed"),
                ),
                redirect_stdout(output),
            ):
                result = main(["convert", "--vault", str(vault), "--list"])
            self.assertEqual(result, 0)
            self.assertIn("PAPER001  Ready", output.getvalue())

    def test_cli_bare_convert_runs_picker_then_loads_token_and_converts(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            ready = ConversionCandidate("PAPER001", vault / "Alpha", "Ready", "可以转换")
            summary = ConversionSummary(selected=1, converted=1)
            events = []
            output = io.StringIO()

            with (
                patch("paper_marrow.cli.supports_tui", return_value=True),
                patch("paper_marrow.cli.Zotero") as zotero_type,
                patch("paper_marrow.cli.ConversionService") as service_type,
                patch(
                    "paper_marrow.cli.run_conversion_picker",
                    side_effect=lambda candidates: events.append(("picker", candidates))
                    or ["PAPER001"],
                ),
                patch(
                    "paper_marrow.cli.load_vault_token",
                    side_effect=lambda _vault: events.append(("token", None)) or "token",
                ),
                redirect_stdout(output),
            ):
                service = service_type.return_value
                service.list_candidates.side_effect = (
                    lambda: events.append(("list", None)) or [ready]
                )
                service.convert_selected.side_effect = (
                    lambda keys, token: events.append(("convert", (keys, token))) or summary
                )
                result = main(["convert", "--vault", str(vault)])

            self.assertEqual(result, 0)
            self.assertEqual(
                events,
                [
                    ("list", None),
                    ("picker", [ready]),
                    ("token", None),
                    ("convert", (["PAPER001"], "token")),
                ],
            )
            self.assertIn("正在检查论文转换状态", output.getvalue())
            zotero_type.assert_not_called()

    def test_cli_bare_convert_cancel_never_loads_token(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            ready = ConversionCandidate("PAPER001", vault / "Alpha", "Ready", "可以转换")
            with (
                patch("paper_marrow.cli.supports_tui", return_value=True),
                patch("paper_marrow.cli.Zotero") as zotero_type,
                patch("paper_marrow.cli.ConversionService") as service_type,
                patch("paper_marrow.cli.run_conversion_picker", return_value=None),
                patch(
                    "paper_marrow.cli.load_vault_token",
                    side_effect=AssertionError("token must not be loaded"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                service_type.return_value.list_candidates.return_value = [ready]
                result = main(["convert", "--vault", str(vault)])
            self.assertEqual(result, 130)
            service_type.return_value.convert_selected.assert_not_called()
            zotero_type.assert_not_called()

    def test_cli_bare_convert_without_papers_returns_without_token(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            output = io.StringIO()
            with (
                patch("paper_marrow.cli.supports_tui", return_value=True),
                patch("paper_marrow.cli.Zotero") as zotero_type,
                patch("paper_marrow.cli.ConversionService") as service_type,
                patch(
                    "paper_marrow.cli.run_conversion_picker",
                    side_effect=AssertionError("picker must not run"),
                ),
                patch(
                    "paper_marrow.cli.load_vault_token",
                    side_effect=AssertionError("token must not be loaded"),
                ),
                redirect_stdout(output),
            ):
                service_type.return_value.list_candidates.return_value = []
                result = main(["convert", "--vault", str(vault)])
            self.assertEqual(result, 0)
            self.assertIn("没有已完成入库的论文", output.getvalue())
            zotero_type.assert_not_called()

    def test_cli_bare_convert_rejects_non_interactive_terminal_before_zotero(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            output = io.StringIO()
            with (
                patch("paper_marrow.cli.supports_tui", return_value=False),
                patch(
                    "paper_marrow.cli.Zotero",
                    side_effect=AssertionError("Zotero must not be accessed"),
                ),
                redirect_stderr(output),
            ):
                result = main(["convert", "--vault", str(vault)])
            self.assertEqual(result, 1)
            self.assertIn("convert --list", output.getvalue())
            self.assertIn("convert --key ITEM_KEY", output.getvalue())

    def test_cli_convert_list_and_key_remain_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["convert", "--list", "--key", "PAPER001"])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_cli_link_pdfs_reports_summary(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            vault = root_path / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            folder = vault / "paper"
            folder.mkdir()
            (folder / "meta.md").write_text(render_meta(item()["data"]), encoding="utf-8")
            source = root_path / "source.pdf"
            source.write_bytes(b"%PDF")
            fake = FakeZotero(
                [],
                {"PAPER001": [pdf_attachment("PDFKEY01")]},
                {"PDFKEY01": source.as_uri()},
            )
            output = io.StringIO()
            with patch("paper_marrow.cli.Zotero", return_value=fake), redirect_stdout(output):
                result = main(["link-pdfs", "--vault", str(vault)])
            self.assertEqual(result, 0)
            self.assertIn(
                "papers=1 linked=1 unchanged=0 missing=0 conflicts=0 failed=0",
                output.getvalue(),
            )
            self.assertTrue((folder / "paper [PDFKEY01].pdf").is_symlink())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_doctor_checks_pdf_symlink_capability(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            initialize(vault, "COLLECT1", "http://localhost:23119/api/")
            output = io.StringIO()
            with patch("paper_marrow.cli.Zotero", return_value=FakeZotero()), redirect_stdout(output):
                result = main(["doctor", "--vault", str(vault)])
            self.assertEqual(result, 0)
            self.assertIn("通过：PDF 符号链接", output.getvalue())

    def test_cli_has_no_removed_commands(self):
        for command in ("watch", "status", "select-pdf"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main([command])


if __name__ == "__main__":
    unittest.main()
