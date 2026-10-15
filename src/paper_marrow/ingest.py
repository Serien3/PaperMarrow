"""One-shot transfer from Zotero into an immutable JSON snapshot and meta.md."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .files import (
    PipelineError,
    atomic_create,
    paper_folder_name,
    read_zotero_json,
    render_meta,
    render_zotero_json,
)
from .library import Config, index_existing
from .pdf_links import PdfLinkSummary, PdfLinker
from .zotero import Zotero, key


SYNC_PDF_WORKERS = 4
_PROGRESS_STEPS = 20


@dataclass
class ImportSummary:
    created: int = 0
    skipped: int = 0
    conflicts: int = 0
    failed: int = 0
    pdf_linked: int = 0
    pdf_missing: int = 0
    pdf_failed: int = 0
    created_keys: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return self.conflicts == 0 and self.failed == 0 and self.pdf_failed == 0


class Importer:
    def __init__(self, vault: Path, config: Config, zotero: Zotero):
        self.vault = vault
        self.config = config
        self.zotero = zotero

    def sync(self, progress: Callable[[str], None] | None = None) -> ImportSummary:
        self._report(progress, "Reading Zotero Collection...")
        items = self.zotero.items(self.config.collection)
        if not isinstance(items, list):
            raise PipelineError("Zotero Collection response is not a list of items")
        index = index_existing(self.vault)
        summary = ImportSummary()
        conflict_keys = set(index.conflicts)
        for item_key, paths in sorted(index.conflicts.items()):
            summary.conflicts += 1
            summary.messages.append(f"local key conflict {item_key}: " + ", ".join(str(path) for path in paths))

        candidates: list[tuple[str, dict]] = []
        counts: dict[str, int] = {}
        for position, item in enumerate(items, start=1):
            try:
                item_key, data = self._validate_item(item)
                if data["itemType"] in {"attachment", "note", "annotation"}:
                    summary.skipped += 1
                    continue
                candidates.append((item_key, data))
                counts[item_key] = counts.get(item_key, 0) + 1
            except (PipelineError, OSError, UnicodeError, ValueError, TypeError) as error:
                summary.failed += 1
                summary.messages.append(f"Zotero item {position} failed: {error}")
        for item_key, count in sorted(counts.items()):
            if count > 1 and item_key not in conflict_keys:
                summary.conflicts += 1
                conflict_keys.add(item_key)
                summary.messages.append(f"Zotero response contains duplicate key: {item_key}")

        existing = index.keys
        pdf_tasks: list[tuple[str, Path]] = []
        self._report(progress, f"Processing {len(candidates)} Zotero items...")
        for item_key, data in candidates:
            try:
                if item_key in conflict_keys:
                    continue
                if item_key in existing:
                    summary.skipped += 1
                    continue
                partial_paths = index.partial.get(item_key, ())
                folder = (
                    partial_paths[0].parent.parent
                    if partial_paths
                    else self.vault / paper_folder_name(item_key, data)
                )
                if folder.is_symlink():
                    raise PipelineError(f"refusing to use a symlinked paper directory: {folder}")
                meta_path = folder / "meta.md"
                json_path = folder / "temp" / "zotero-item.json"
                if meta_path.exists() or meta_path.is_symlink():
                    summary.skipped += 1
                    continue
                if json_path.parent.is_symlink() or (
                    json_path.parent.exists() and not json_path.parent.is_dir()
                ):
                    raise PipelineError(f"temp is not a regular directory: {json_path.parent}")

                source_data = data
                if json_path.exists() or json_path.is_symlink():
                    source_data = read_zotero_json(json_path)
                    self._validate_data(item_key, source_data)
                meta_content = render_meta(source_data)
                json_content = render_zotero_json(source_data)

                if not json_path.exists() and not json_path.is_symlink():
                    try:
                        atomic_create(json_path, json_content)
                    except FileExistsError:
                        source_data = read_zotero_json(json_path)
                        self._validate_data(item_key, source_data)
                        meta_content = render_meta(source_data)
                try:
                    atomic_create(meta_path, meta_content)
                except FileExistsError:
                    summary.skipped += 1
                    existing.add(item_key)
                    continue
                existing.add(item_key)
                summary.created += 1
                summary.created_keys.append(item_key)
                pdf_tasks.append((item_key, folder))
            except (PipelineError, OSError, UnicodeError, ValueError, TypeError) as error:
                summary.failed += 1
                summary.messages.append(f"failed to import Zotero item {item_key}: {error}")
        self._link_created_papers(pdf_tasks, summary, progress)
        return summary

    def _link_created_papers(
        self,
        tasks: list[tuple[str, Path]],
        summary: ImportSummary,
        progress: Callable[[str], None] | None,
    ) -> None:
        total = len(tasks)
        if total == 0:
            return
        workers = min(SYNC_PDF_WORKERS, total)
        self._report(
            progress,
            f"Linking PDFs for {total} new papers (up to {workers} concurrent)...",
        )
        if workers == 1:
            results = [self._link_paper(*tasks[0])]
            self._report(progress, "PDF link progress: 1/1")
        else:
            results: list[PdfLinkSummary | None] = [None] * total
            completed = 0
            interval = max(1, (total + _PROGRESS_STEPS - 1) // _PROGRESS_STEPS)
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sync-pdf")
            futures: dict[Future[PdfLinkSummary], int] = {}
            try:
                for position, task in enumerate(tasks):
                    futures[executor.submit(self._link_paper, *task)] = position
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
                    completed += 1
                    if completed == total or completed % interval == 0:
                        self._report(progress, f"PDF link progress: {completed}/{total}")
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)

        for (item_key, _), result in zip(tasks, results):
            if result is None:
                raise PipelineError(f"PDF link task for paper {item_key} returned no result")
            summary.pdf_linked += result.linked
            summary.pdf_missing += result.missing
            summary.pdf_failed += result.failed + result.conflicts
            summary.messages.extend(result.messages)

    def _link_paper(self, item_key: str, folder: Path) -> PdfLinkSummary:
        try:
            session = self.zotero.new_session()
            return PdfLinker(session).link_paper(folder, item_key)
        except (PipelineError, OSError, UnicodeError, ValueError, TypeError) as error:
            return PdfLinkSummary(
                papers=1,
                failed=1,
                messages=[f"cannot read attachments for paper {item_key}: {error}"],
            )

    @staticmethod
    def _report(progress: Callable[[str], None] | None, message: str) -> None:
        if progress is not None:
            progress(message)

    @staticmethod
    def _validate_item(item: object) -> tuple[str, dict]:
        if not isinstance(item, dict):
            raise PipelineError("item is not a JSON object")
        outer_key = key(item.get("key", ""))
        data = item.get("data")
        if not isinstance(data, dict):
            raise PipelineError(f"item {outer_key} lacks a data object")
        inner_key = key(data.get("key", ""))
        if inner_key != outer_key:
            raise PipelineError(f"item outer key does not match data.key: {outer_key} / {inner_key}")
        Importer._validate_data(outer_key, data)
        return outer_key, data

    @staticmethod
    def _validate_data(expected_key: str, data: dict) -> None:
        inner_key = key(data.get("key", ""))
        if inner_key != expected_key:
            raise PipelineError(f"Zotero data.key does not match the target key: {inner_key} / {expected_key}")
        if not isinstance(data.get("itemType"), str) or not data["itemType"].strip():
            raise PipelineError(f"item {expected_key} lacks a valid itemType")
