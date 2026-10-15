"""Create zero-copy PDF symlinks to files managed by Zotero."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .files import PipelineError
from .library import index_existing
from .zotero import Zotero, key


_INVALID_FILENAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')
_WHITESPACE = re.compile(r"\s+")
_FILE_LINK_MODES = {"imported_file", "imported_url", "linked_file"}


@dataclass
class PdfLinkSummary:
    papers: int = 0
    linked: int = 0
    unchanged: int = 0
    missing: int = 0
    conflicts: int = 0
    failed: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return self.conflicts == 0 and self.failed == 0


@dataclass(frozen=True)
class PdfAttachment:
    key: str
    filename: str


def pdf_link_name(filename: str, attachment_key: str) -> str:
    if not isinstance(filename, str):
        raise PipelineError("Zotero attachment.filename must be a string")
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    stem = _INVALID_FILENAME.sub(" - ", stem)
    stem = _WHITESPACE.sub(" ", stem).strip(" .-")
    stem = stem[:100].rstrip(" .-") or "PDF"
    return f"{stem} [{attachment_key}].pdf"


def file_uri_path(uri: str) -> Path:
    if not isinstance(uri, str) or not uri:
        raise PipelineError("Zotero returned no attachment file URL")
    parts = urlsplit(uri)
    if parts.scheme.lower() != "file" or parts.query or parts.fragment:
        raise PipelineError(f"Zotero attachment URL is not a plain file URI: {uri}")
    decoded = unquote(parts.path)
    if os.name == "nt":
        if parts.netloc and parts.netloc.lower() != "localhost":
            decoded = f"//{parts.netloc}{decoded}"
        elif re.match(r"^/[A-Za-z]:/", decoded):
            decoded = decoded[1:]
        decoded = decoded.replace("/", "\\")
    elif parts.netloc and parts.netloc.lower() != "localhost":
        decoded = f"//{parts.netloc}{decoded}"
    path = Path(decoded)
    if not path.is_absolute():
        raise PipelineError(f"Zotero attachment URL is not an absolute path: {uri}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PipelineError(f"Zotero PDF is not downloaded or the file is missing: {path}") from error
    if not resolved.is_file():
        raise PipelineError(f"Zotero PDF target is not a regular file: {resolved}")
    return resolved


def _attachment(child: object, parent_key: str) -> PdfAttachment | None:
    if not isinstance(child, dict):
        raise PipelineError("Zotero child item is not an object")
    data = child.get("data")
    if not isinstance(data, dict):
        raise PipelineError("Zotero child item lacks a data object")
    if data.get("itemType") != "attachment":
        return None
    if data.get("parentItem") != parent_key:
        raise PipelineError("Zotero attachment.parentItem does not match the parent item")
    attachment_key = key(data.get("key", ""))
    if child.get("key") != attachment_key:
        raise PipelineError("Zotero attachment outer key does not match data.key")
    link_mode = data.get("linkMode")
    if link_mode not in _FILE_LINK_MODES:
        return None
    filename = data.get("filename", "")
    content_type = data.get("contentType", "")
    if not isinstance(filename, str) or not isinstance(content_type, str):
        raise PipelineError(f"Zotero attachment {attachment_key} has invalid file fields")
    if content_type.lower() != "application/pdf" and not filename.lower().endswith(".pdf"):
        return None
    return PdfAttachment(attachment_key, filename)


def _same_file(left: Path, right: Path) -> bool:
    """Return whether two paths identify the same file, following symlinks."""
    try:
        return left.samefile(right)
    except OSError:
        return False


def _atomic_replace_symlink(link: Path, target: Path) -> None:
    temporary = link.parent / f".{link.name}.{uuid.uuid4().hex}.tmp"
    try:
        os.symlink(target, temporary, target_is_directory=False)
        os.replace(temporary, link)
    finally:
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink()


def _create_symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=False)
    except FileExistsError:
        raise
    except OSError as error:
        raise PipelineError(f"cannot create PDF symlink {link}; on Windows enable Developer Mode") from error


def ensure_pdf_link(folder: Path, attachment: PdfAttachment, target: Path) -> str:
    desired = folder / pdf_link_name(attachment.filename, attachment.key)
    suffix = f" [{attachment.key}].pdf".casefold()
    try:
        managed = [path for path in folder.iterdir() if path.is_symlink() and path.name.casefold().endswith(suffix)]
    except OSError as error:
        raise PipelineError(f"cannot scan PDF links in paper directory: {folder}") from error
    if len(managed) > 1:
        raise FileExistsError(f"multiple PDF links declare the same attachment key: {attachment.key}")

    if managed:
        current = managed[0]
        if current != desired and (desired.exists() or desired.is_symlink()):
            raise FileExistsError(f"PDF link target name already exists: {desired}")
        if current != desired:
            _create_symlink(desired, target)
            try:
                current.unlink()
            except OSError:
                desired.unlink(missing_ok=True)
                raise
            return "linked"
        if not _same_file(current, target):
            _atomic_replace_symlink(current, target)
            return "linked"
        return "unchanged"

    if desired.exists() or desired.is_symlink():
        raise FileExistsError(f"PDF link target name already exists: {desired}")
    _create_symlink(desired, target)
    return "linked"


class PdfLinker:
    def __init__(self, zotero: Zotero):
        self.zotero = zotero

    def link_paper(self, folder: Path, parent_key: str) -> PdfLinkSummary:
        summary = PdfLinkSummary(papers=1)
        try:
            children = self.zotero.children(parent_key)
            if not isinstance(children, list):
                raise PipelineError("Zotero children response is not a list")
        except (PipelineError, OSError, UnicodeError, ValueError, TypeError) as error:
            summary.failed += 1
            summary.messages.append(f"cannot read attachments for paper {parent_key}: {error}")
            return summary

        attachments: list[PdfAttachment] = []
        for position, child in enumerate(children, start=1):
            try:
                attachment = _attachment(child, parent_key)
                if attachment is not None:
                    attachments.append(attachment)
            except (PipelineError, OSError, UnicodeError, ValueError, TypeError) as error:
                summary.failed += 1
                summary.messages.append(f"attachment {position} of paper {parent_key} failed: {error}")
        if not attachments:
            if summary.failed == 0:
                summary.missing += 1
            return summary

        for attachment in attachments:
            try:
                target = file_uri_path(self.zotero.attachment_file_url(attachment.key))
                outcome = ensure_pdf_link(folder, attachment, target)
                if outcome == "linked":
                    summary.linked += 1
                else:
                    summary.unchanged += 1
            except FileExistsError as error:
                summary.conflicts += 1
                summary.messages.append(f"PDF link conflict for paper {parent_key}: {error}")
            except (PipelineError, OSError, UnicodeError, ValueError, TypeError) as error:
                summary.failed += 1
                summary.messages.append(f"attachment {attachment.key} of paper {parent_key} failed: {error}")
        return summary

    def link_existing(self, vault: Path) -> PdfLinkSummary:
        index = index_existing(vault)
        summary = PdfLinkSummary()
        conflict_keys = set(index.conflicts)
        for item_key, paths in sorted(index.conflicts.items()):
            summary.conflicts += 1
            summary.messages.append(f"local key conflict {item_key}: " + ", ".join(str(path) for path in paths))
        for item_key, paths in sorted(index.complete.items()):
            if item_key in conflict_keys:
                continue
            result = self.link_paper(paths[0].parent, item_key)
            summary.papers += result.papers
            summary.linked += result.linked
            summary.unchanged += result.unchanged
            summary.missing += result.missing
            summary.conflicts += result.conflicts
            summary.failed += result.failed
            summary.messages.extend(result.messages)
        return summary
