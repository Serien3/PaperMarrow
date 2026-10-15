"""Configuration and local-vault indexing for import-once metadata."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .files import PipelineError, atomic_create, read_meta, read_zotero_json
from .zotero import key, validate_url


@dataclass(frozen=True)
class Pdf2MdConfig:
    model: str = "vlm"
    language: str = "en"
    ocr: bool = False
    timeout: int = 1800


@dataclass(frozen=True)
class Config:
    collection: str
    zotero_url: str
    pdf2md: Pdf2MdConfig = field(default_factory=Pdf2MdConfig)


@dataclass(frozen=True)
class ExistingIndex:
    complete: dict[str, tuple[Path, ...]]
    partial: dict[str, tuple[Path, ...]]

    @property
    def keys(self) -> set[str]:
        return set(self.complete)

    @property
    def conflicts(self) -> dict[str, tuple[Path, ...]]:
        declarations: dict[str, tuple[Path, ...]] = {}
        for item_key in self.complete.keys() | self.partial.keys():
            paths = self.complete.get(item_key, ()) + self.partial.get(item_key, ())
            if len(paths) > 1:
                declarations[item_key] = paths
        return declarations


def initialize(
    vault: Path,
    collection: str,
    zotero_url: str,
    *,
    pdf2md_model: str = "vlm",
    pdf2md_language: str = "en",
    pdf2md_ocr: bool = False,
    pdf2md_timeout: int = 1800,
) -> None:
    collection = key(collection)
    zotero_url = validate_url(zotero_url)
    vault.mkdir(parents=True, exist_ok=True)
    if vault.is_symlink() or not vault.is_dir():
        raise PipelineError(f"vault is not a regular directory: {vault}")
    path = vault / ".papermarrow" / "config.toml"
    pdf2md = _pdf2md_config(
        {
            "model": pdf2md_model,
            "language": pdf2md_language,
            "ocr": pdf2md_ocr,
            "timeout": pdf2md_timeout,
        }
    )
    content = f"collection = {json.dumps(collection)}\nzotero_url = {json.dumps(zotero_url)}\n"
    content += (
        "\n[pdf2md]\n"
        f"model = {json.dumps(pdf2md.model)}\n"
        f"language = {json.dumps(pdf2md.language)}\n"
        f"ocr = {str(pdf2md.ocr).lower()}\n"
        f"timeout = {pdf2md.timeout}\n"
    )
    try:
        atomic_create(path, content)
    except FileExistsError as error:
        raise PipelineError(f"configuration already exists; init will not overwrite: {path}") from error


def load_config(vault: Path) -> Config:
    path = vault / ".papermarrow" / "config.toml"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PipelineError(f"cannot read configuration; run init first: {path}") from error
    expected = {"collection", "zotero_url", "pdf2md"}
    if not {"collection", "zotero_url"}.issubset(config) or not set(config) <= expected:
        raise PipelineError("configuration must contain collection and zotero_url, and may only additionally contain [pdf2md]")
    collection = key(config.get("collection", ""))
    zotero_url = config.get("zotero_url")
    if not isinstance(zotero_url, str):
        raise PipelineError("zotero_url must be a string")
    validate_url(zotero_url)
    raw_pdf2md = config.get("pdf2md", {})
    if not isinstance(raw_pdf2md, dict):
        raise PipelineError("pdf2md must be a TOML table")
    return Config(
        collection=collection,
        zotero_url=zotero_url,
        pdf2md=_pdf2md_config(raw_pdf2md),
    )


def _pdf2md_config(raw: dict) -> Pdf2MdConfig:
    # ``enabled`` existed briefly during v2 development. Accept and ignore it
    # so a generated config cannot accidentally re-couple conversion to sync.
    allowed = {"enabled", "model", "language", "ocr", "timeout"}
    if not set(raw) <= allowed:
        unknown = "、".join(sorted(set(raw) - allowed))
        raise PipelineError(f"pdf2md contains unknown settings: {unknown}")

    deprecated_enabled = raw.get("enabled", False)
    model = raw.get("model", "vlm")
    language = raw.get("language", "en")
    ocr = raw.get("ocr", False)
    timeout = raw.get("timeout", 1800)
    if not isinstance(deprecated_enabled, bool):
        raise PipelineError("deprecated pdf2md.enabled must be a boolean")
    if model not in {"vlm", "pipeline"}:
        raise PipelineError("pdf2md.model must be vlm or pipeline")
    if not isinstance(language, str) or not language.strip():
        raise PipelineError("pdf2md.language must be a non-empty string")
    if not isinstance(ocr, bool):
        raise PipelineError("pdf2md.ocr must be a boolean")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise PipelineError("pdf2md.timeout must be a positive integer")
    return Pdf2MdConfig(
        model=model,
        language=language.strip(),
        ocr=ocr,
        timeout=timeout,
    )


def index_existing(vault: Path) -> ExistingIndex:
    """Index complete and interrupted imports in direct-child folders."""
    complete: dict[str, list[Path]] = {}
    partial: dict[str, list[Path]] = {}
    try:
        children = list(vault.iterdir())
    except OSError as error:
        raise PipelineError(f"cannot scan vault: {vault}") from error
    for folder in children:
        if folder.is_symlink() or not folder.is_dir():
            continue
        meta_path = folder / "meta.md"
        temp_path = folder / "temp"
        sidecar_path = temp_path / "zotero-item.json"
        if meta_path.exists() or meta_path.is_symlink():
            fields = read_meta(meta_path)
            item_key = fields.get("key")
            path = meta_path
            destination = complete
        elif sidecar_path.exists() or sidecar_path.is_symlink():
            if temp_path.is_symlink() or not temp_path.is_dir():
                raise PipelineError(f"temp is not a regular directory: {temp_path}")
            fields = read_zotero_json(sidecar_path)
            item_key = fields.get("key")
            path = sidecar_path
            destination = partial
        else:
            continue
        if not isinstance(item_key, str):
            raise PipelineError(f"existing metadata file lacks a Zotero string key: {path}")
        try:
            item_key = key(item_key)
        except PipelineError as error:
            raise PipelineError(f"existing metadata file has an invalid Zotero key: {path}") from error
        destination.setdefault(item_key, []).append(path)
    return ExistingIndex(
        {item_key: tuple(paths) for item_key, paths in complete.items()},
        {item_key: tuple(paths) for item_key, paths in partial.items()},
    )
