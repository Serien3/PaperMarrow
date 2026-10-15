"""Explicit, quota-conscious orchestration for selected PDF conversions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .files import PipelineError, PdfConversionLock, VaultLock
from .library import Config, ExistingIndex, index_existing
from .pdf2md import ConversionOptions, convert_pdf_into_paper
from .zotero import key


_MANAGED_PDF_LINK = re.compile(r"^.+ \[[A-Z0-9]{8}\]\.pdf$")


@dataclass(frozen=True)
class ConversionCandidate:
    item_key: str
    paper_folder: Path
    status: str
    detail: str
    pdf: Path | None = None


@dataclass
class ConversionSummary:
    selected: int = 0
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return self.failed == 0


class ConversionService:
    def __init__(self, vault: Path, config: Config):
        self.vault = vault
        self.config = config

    def _index(self) -> ExistingIndex:
        with VaultLock(self.vault):
            return index_existing(self.vault)

    def inspect(self, item_key: str, folder: Path) -> ConversionCandidate:
        full_markdown = folder / "full.md"
        if full_markdown.is_file() and not full_markdown.is_symlink():
            return ConversionCandidate(item_key, folder, "Converted", "full.md 已存在")
        if full_markdown.exists() or full_markdown.is_symlink():
            return ConversionCandidate(item_key, folder, "Unavailable", "full.md 不是普通文件")
        images = folder / "images"
        if images.exists() or images.is_symlink():
            return ConversionCandidate(
                item_key,
                folder,
                "Unavailable",
                "已有转换目标：images",
            )
        temp = folder / "temp"
        if temp.is_symlink() or (temp.exists() and not temp.is_dir()):
            return ConversionCandidate(item_key, folder, "Unavailable", "temp 不是普通目录")
        try:
            available = self._local_pdf(folder)
        except (PipelineError, OSError) as error:
            return ConversionCandidate(item_key, folder, "Unavailable", str(error))
        return ConversionCandidate(item_key, folder, "Ready", "可以转换", available)

    @staticmethod
    def _local_pdf(folder: Path) -> Path:
        if folder.is_symlink() or not folder.is_dir():
            raise PipelineError(f"论文目录不是普通目录：{folder}")
        try:
            managed = [
                path
                for path in folder.iterdir()
                if path.is_symlink() and _MANAGED_PDF_LINK.fullmatch(path.name)
            ]
        except OSError as error:
            raise PipelineError(f"无法扫描论文目录中的 PDF 链接：{folder}") from error
        if not managed:
            raise PipelineError("没有受管理的 PDF 符号链接；请先运行 link-pdfs")
        if len(managed) > 1:
            raise PipelineError(f"有 {len(managed)} 个受管理的 PDF 符号链接，无法确定转换对象")

        link = managed[0]
        try:
            target = link.resolve(strict=True)
        except OSError as error:
            raise PipelineError(f"PDF 符号链接已经失效：{link}") from error
        if not target.is_file():
            raise PipelineError(f"PDF 符号链接目标不是普通文件：{link}")
        return link

    def list_candidates(self) -> list[ConversionCandidate]:
        index = self._index()
        conflicts = set(index.conflicts)
        candidates: list[ConversionCandidate] = []
        for item_key, paths in sorted(index.complete.items()):
            folder = paths[0].parent
            if item_key in conflicts:
                candidates.append(
                    ConversionCandidate(
                        item_key,
                        folder,
                        "Unavailable",
                        "多个论文目录声明同一 key",
                    )
                )
            else:
                candidates.append(self.inspect(item_key, folder))
        return candidates

    def convert_selected(self, requested_keys: list[str], token: str) -> ConversionSummary:
        index = self._index()
        summary = ConversionSummary()
        unique_keys: list[str] = []
        seen: set[str] = set()
        for value in requested_keys:
            summary.selected += 1
            try:
                item_key = key(value)
            except PipelineError as error:
                summary.failed += 1
                summary.messages.append(f"无效论文 key {value!r}：{error}")
                continue
            if item_key in seen:
                summary.skipped += 1
                summary.messages.append(f"论文 {item_key} 重复选择，已跳过")
                continue
            seen.add(item_key)
            unique_keys.append(item_key)

        conflicts = set(index.conflicts)
        for item_key in unique_keys:
            if item_key in conflicts:
                summary.failed += 1
                summary.messages.append(f"论文 {item_key} 有多个本地目录声明，无法转换")
                continue
            paths = index.complete.get(item_key, ())
            if not paths:
                summary.failed += 1
                summary.messages.append(f"论文 {item_key} 尚未完成入库")
                continue
            folder = paths[0].parent
            try:
                with PdfConversionLock(self.vault, item_key):
                    candidate = self.inspect(item_key, folder)
                    if candidate.status == "Converted":
                        summary.skipped += 1
                        summary.messages.append(f"论文 {item_key} 已有 full.md，未调用 MinerU")
                        continue
                    if candidate.status != "Ready" or candidate.pdf is None:
                        raise PipelineError(f"{candidate.status}：{candidate.detail}")
                    options = ConversionOptions(
                        pdf=candidate.pdf,
                        output_root=folder,
                        token=token,
                        model=self.config.pdf2md.model,
                        language=self.config.pdf2md.language,
                        ocr=self.config.pdf2md.ocr,
                        timeout=self.config.pdf2md.timeout,
                    )
                    convert_pdf_into_paper(options, folder)
                summary.converted += 1
            except (PipelineError, OSError, UnicodeError, ValueError, TypeError) as error:
                summary.failed += 1
                summary.messages.append(f"论文 {item_key} 转换失败：{error}")
        return summary
