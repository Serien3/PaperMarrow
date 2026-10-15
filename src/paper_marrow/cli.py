"""Command-line interface for one-shot Zotero paper import."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .conversion import ConversionService
from .conversion_tui import run_conversion_picker, supports_tui
from .files import PipelineError, VaultLock
from .ingest import Importer
from .library import initialize, load_config
from .pdf2md import load_vault_token
from .pdf_links import PdfLinker
from .zotero import Zotero


DEFAULT_ZOTERO_URL = "http://localhost:23119/api/"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="将 Zotero Collection 元数据一次性导入 Obsidian vault")
    commands = result.add_subparsers(dest="command", required=True)

    collections = commands.add_parser("collections", help="列出 Zotero 个人库的 Collection")
    collections.add_argument("--zotero-url", default=DEFAULT_ZOTERO_URL)

    init = commands.add_parser("init", help="在已有或新建 vault 中初始化最小配置")
    init.add_argument("--vault", type=Path, help="vault 路径（默认：当前目录）")
    init.add_argument("--collection", required=True, help="Zotero Collection 的 8 位 key")
    init.add_argument("--zotero-url", default=DEFAULT_ZOTERO_URL)
    init.add_argument("--pdf2md-model", choices=("vlm", "pipeline"), default="vlm")
    init.add_argument("--pdf2md-language", default="en")
    init.add_argument("--pdf2md-ocr", action="store_true")
    init.add_argument("--pdf2md-timeout", type=int, default=1800)

    doctor = commands.add_parser("doctor", help="检查配置、vault 和 Zotero 连接")
    doctor.add_argument("--vault", type=Path, help="vault 路径（默认：从当前目录向上发现）")

    sync = commands.add_parser("sync", help="导入尚未入库的 Zotero 元数据")
    sync.add_argument("--vault", type=Path, help="vault 路径（默认：从当前目录向上发现）")
    link_pdfs = commands.add_parser("link-pdfs", help="为已入库论文补建或修复 Zotero PDF 链接")
    link_pdfs.add_argument(
        "--vault", type=Path, help="vault 路径（默认：从当前目录向上发现）"
    )
    convert = commands.add_parser("convert", help="交互选择、列出或转换已入库论文")
    convert.add_argument("--vault", type=Path, help="vault 路径（默认：从当前目录向上发现）")
    mode = convert.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="列出所有论文的转换就绪状态")
    mode.add_argument(
        "--key",
        action="append",
        dest="keys",
        metavar="ITEM_KEY",
        help="转换指定 Zotero item key；可重复使用",
    )
    return result


def _vault(value: Path | None, *, for_init: bool = False) -> Path:
    """Resolve an explicit vault or discover the nearest initialized ancestor."""
    try:
        if value is not None:
            return value.expanduser().resolve()
        current = Path.cwd().resolve()
    except (OSError, RuntimeError) as error:
        raise PipelineError(f"无法解析 vault 路径：{error}") from error

    if for_init:
        return current
    for candidate in (current, *current.parents):
        marker = candidate / ".papermarrow" / "config.toml"
        if marker.exists() or marker.is_symlink():
            return candidate
    raise PipelineError(
        f"无法从当前目录向上找到 .papermarrow/config.toml：{current}；"
        "请进入已初始化的 vault，或显式提供 --vault"
    )


def doctor(vault: Path) -> int:
    errors: list[str] = []
    try:
        config = load_config(vault)
        print("通过：配置")
    except PipelineError as error:
        print(f"待处理：配置 — {error}")
        return 1
    try:
        load_vault_token(vault)
        print("通过：MinerU Token（convert 可用）")
    except PipelineError as error:
        print(f"提示：MinerU Token 未就绪，sync 不受影响 — {error}")
    try:
        with tempfile.TemporaryFile(dir=vault) as handle:
            handle.write(b"check")
        print("通过：vault 可写")
    except OSError as error:
        errors.append(str(error))
        print(f"待处理：vault 不可写 — {error}")
    try:
        with tempfile.TemporaryDirectory(dir=vault) as directory:
            source = Path(directory) / "source.pdf"
            link = Path(directory) / "link.pdf"
            source.write_bytes(b"check")
            os.symlink(source, link, target_is_directory=False)
            if not link.is_symlink():
                raise OSError("创建结果不是符号链接")
        print("通过：PDF 符号链接")
    except OSError as error:
        errors.append(str(error))
        print(f"待处理：PDF 符号链接 — {error}（Windows 请开启开发者模式）")
    try:
        Zotero(config.zotero_url).items(config.collection)
        print("通过：Zotero 与 Collection")
    except (PipelineError, OSError) as error:
        errors.append(str(error))
        print(f"待处理：Zotero 与 Collection — {error}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parser().parse_args(argv)
    try:
        if args.command == "collections":
            for collection in Zotero(args.zotero_url).collections():
                data = collection.get("data", {})
                parent = data.get("parentCollection") or "root"
                print(f"{collection.get('key', '')}  {data.get('name', '')}  (parent: {parent})")
            return 0

        vault = _vault(args.vault, for_init=args.command == "init")
        if args.command == "init":
            vault.mkdir(parents=True, exist_ok=True)
            with VaultLock(vault):
                initialize(
                    vault,
                    args.collection,
                    args.zotero_url,
                    pdf2md_model=args.pdf2md_model,
                    pdf2md_language=args.pdf2md_language,
                    pdf2md_ocr=args.pdf2md_ocr,
                    pdf2md_timeout=args.pdf2md_timeout,
                )
            print(f"已初始化：{vault}")
            return 0
        if args.command == "doctor":
            return doctor(vault)

        config = load_config(vault)
        if args.command == "convert":
            if not args.list and args.keys is None and not supports_tui():
                raise PipelineError(
                    "当前终端不支持交互界面；请使用 convert --list 或 "
                    "convert --key ITEM_KEY"
                )
            service = ConversionService(vault, config)
            if args.list:
                for candidate in service.list_candidates():
                    print(
                        f"{candidate.item_key}  {candidate.status:<11}  "
                        f"{candidate.paper_folder.name}  ({candidate.detail})"
                    )
                return 0
            if args.keys is None:
                print("正在检查论文转换状态……", flush=True)
                candidates = service.list_candidates()
                if not candidates:
                    print("没有已完成入库的论文。")
                    return 0
                selected = run_conversion_picker(candidates)
                if selected is None:
                    print("已取消。", file=sys.stderr)
                    return 130
                args.keys = selected
            token = load_vault_token(vault)
            summary = service.convert_selected(args.keys, token)
            for message in summary.messages:
                print(message, file=sys.stderr)
            print(
                f"转换完成：selected={summary.selected} converted={summary.converted} "
                f"skipped={summary.skipped} failed={summary.failed}"
            )
            return 0 if summary.successful else 1
        if args.command == "link-pdfs":
            with VaultLock(vault):
                summary = PdfLinker(Zotero(config.zotero_url)).link_existing(vault)
            for message in summary.messages:
                print(message, file=sys.stderr)
            print(
                f"PDF 链接完成：papers={summary.papers} linked={summary.linked} "
                f"unchanged={summary.unchanged} missing={summary.missing} "
                f"conflicts={summary.conflicts} failed={summary.failed}"
            )
            return 0 if summary.successful else 1
        with VaultLock(vault):
            summary = Importer(vault, config, Zotero(config.zotero_url)).sync(
                progress=lambda message: print(message, file=sys.stderr, flush=True)
            )
        for message in summary.messages:
            print(message, file=sys.stderr)
        print(
            f"同步完成：created={summary.created} skipped={summary.skipped} "
            f"conflicts={summary.conflicts} failed={summary.failed} "
            f"pdf_linked={summary.pdf_linked} pdf_missing={summary.pdf_missing} "
            f"pdf_failed={summary.pdf_failed}"
        )
        if summary.created_keys:
            print("本轮新增 key：" + " ".join(summary.created_keys))
        return 0 if summary.successful else 1
    except (PipelineError, OSError, ValueError, TypeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        return 130
