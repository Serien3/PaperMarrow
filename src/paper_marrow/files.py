"""Filesystem primitives for safe, create-only metadata import."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path

import yaml


class PipelineError(RuntimeError):
    pass


_INVALID_FILENAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')
_WHITESPACE = re.compile(r"\s+")


def _same_value(left: object, right: object) -> bool:
    """Compare JSON-shaped values without conflating bools, ints, and floats."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def _creator_name(creator: object) -> str:
    if not isinstance(creator, dict):
        raise PipelineError("Zotero creator must be an object")
    name = creator.get("name")
    if name is not None and not isinstance(name, str):
        raise PipelineError("Zotero creator.name must be a string")
    if isinstance(name, str) and name.strip():
        return name
    parts = []
    for field in ("firstName", "lastName"):
        value = creator.get(field, "")
        if not isinstance(value, str):
            raise PipelineError(f"Zotero creator.{field} must be a string")
        if value.strip():
            parts.append(value.strip())
    if not parts:
        raise PipelineError("Zotero creator has no displayable name")
    return " ".join(parts)


def project_meta(data: dict) -> dict:
    """Make only the two projections required by Obsidian Properties."""
    if "tags" in data and "zoteroTags" in data:
        raise PipelineError("Zotero data contains both tags and zoteroTags; cannot project losslessly")
    projected = {}
    for field, value in data.items():
        if field == "creators":
            if not isinstance(value, list):
                raise PipelineError("Zotero creators must be an array")
            projected[field] = [_creator_name(creator) for creator in value]
        elif field == "tags":
            if not isinstance(value, list):
                raise PipelineError("Zotero tags must be an array")
            tags = []
            for tag in value:
                if not isinstance(tag, dict) or not isinstance(tag.get("tag"), str) or not tag["tag"]:
                    raise PipelineError("Zotero tag must be an object with a non-empty tag string")
                tags.append(tag["tag"])
            projected["zoteroTags"] = tags
        else:
            projected[field] = copy.deepcopy(value)
    return projected


def paper_folder_name(item_key: str, data: dict) -> str:
    """Build a readable, stable-on-import Windows-safe directory name."""
    for field in ("shortTitle", "title"):
        if field in data and not isinstance(data[field], str):
            raise PipelineError(f"Zotero {field} must be a string")

    display_title = ""
    for field in ("shortTitle", "title"):
        if field not in data:
            continue
        value = data[field]
        if value.strip():
            display_title = value
            break
    if not display_title:
        return item_key

    label = _INVALID_FILENAME.sub(" - ", display_title)
    label = _WHITESPACE.sub(" ", label).strip(" .-")
    label = label[:100].rstrip(" .-")
    return f"{label} [{item_key}]" if label else item_key


def render_meta(data: dict) -> str:
    """Render the minimal Obsidian-compatible projection as YAML frontmatter."""
    projected = project_meta(data)
    try:
        header = yaml.safe_dump(projected, allow_unicode=True, sort_keys=False, width=1000)
        loaded = yaml.safe_load(header)
    except yaml.YAMLError as error:
        raise PipelineError(f"Zotero metadata cannot be rendered as YAML: {error}") from error
    if not _same_value(loaded, projected):
        raise PipelineError("Zotero metadata changed in type or content after YAML rendering")
    return f"---\n{header}---\n"


def render_zotero_json(data: dict) -> str:
    """Serialize Zotero item.data without changing its JSON-shaped value."""
    try:
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        loaded = json.loads(content)
    except (TypeError, ValueError) as error:
        raise PipelineError(f"Zotero metadata cannot be rendered as JSON: {error}") from error
    if not _same_value(loaded, data):
        raise PipelineError("Zotero metadata changed in type or content after JSON rendering")
    return content


def read_zotero_json(path: Path) -> dict:
    if path.is_symlink():
        raise PipelineError(f"refusing to read symlinked zotero-item.json: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise PipelineError(f"cannot read existing zotero-item.json: {path}") from error
    if not isinstance(data, dict):
        raise PipelineError(f"existing zotero-item.json must be a JSON object: {path}")
    return data


def read_meta(path: Path) -> dict:
    """Read a metadata frontmatter mapping without changing the file."""
    if path.is_symlink():
        raise PipelineError(f"refusing to read symlinked meta.md: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise PipelineError(f"cannot read existing meta.md: {path}") from error
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise PipelineError(f"existing meta.md lacks YAML frontmatter: {path}")
    closing = next(
        (index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == "---"),
        None,
    )
    if closing is None:
        raise PipelineError(f"existing meta.md YAML frontmatter is not closed: {path}")
    try:
        fields = yaml.safe_load("".join(lines[1:closing]))
    except yaml.YAMLError as error:
        raise PipelineError(f"existing meta.md has invalid YAML: {path}") from error
    if not isinstance(fields, dict):
        raise PipelineError(f"existing meta.md YAML must be a property mapping: {path}")
    return fields


def atomic_create(path: Path, content: str) -> None:
    """Atomically publish a new UTF-8 file and never replace an existing path."""
    parent = path.parent
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise PipelineError(f"target paper directory is not a writable regular directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise PipelineError(f"refusing to write into a symlinked directory: {parent}")
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)

    encoded = content.encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=parent, prefix=f".{path.name}-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        # A hard link is an atomic create-if-absent operation on NTFS and POSIX
        # filesystems. Unlike os.replace(), it cannot overwrite a user's file.
        os.link(temporary, path)
    except FileExistsError:
        raise
    except OSError as error:
        raise PipelineError(f"cannot safely publish file: {path} (filesystem must support hard links)") from error
    finally:
        temporary.unlink(missing_ok=True)


class VaultLock:
    """OS-managed writer lock, released automatically when the process exits."""

    def __init__(
        self,
        vault: Path,
        *,
        relative_path: Path = Path("writer.lock"),
        busy_message: str = "another Literature Pipeline writer is active in this vault",
    ):
        self.papermarrow_directory = vault / ".papermarrow"
        self.path = vault / ".papermarrow" / relative_path
        self.busy_message = busy_message
        self.handle = None

    def __enter__(self):
        directory = self.path.parent
        if self.papermarrow_directory.exists() and (
            self.papermarrow_directory.is_symlink() or not self.papermarrow_directory.is_dir()
        ):
            raise PipelineError(f".papermarrow is not a writable regular directory: {self.papermarrow_directory}")
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise PipelineError(f".papermarrow is not a writable regular directory: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0, 2)
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            raise PipelineError(self.busy_message) from error
        return self

    def __exit__(self, *_):
        if self.handle:
            self.handle.close()


class PdfConversionLock(VaultLock):
    """Serialize conversion of one Zotero item without locking the whole vault."""

    def __init__(self, vault: Path, item_key: str):
        super().__init__(
            vault,
            relative_path=Path("pdf2md-locks") / f"{item_key}.lock",
            busy_message=f"another PDF conversion is running for paper {item_key}",
        )
