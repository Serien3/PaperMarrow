"""Installed MinerU conversion support for explicit and standalone commands."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .files import PipelineError


MAX_PDF_BYTES = 200 * 1024 * 1024
PIPELINE_OUTPUT_NAMES = ("full.md", "images")


class ConversionError(PipelineError):
    """An expected, user-facing conversion error."""


def _staging_mode() -> int:
    # On Windows, os.mkdir(0o700) creates a private ACL. Files and directories
    # moved or hard-linked out of it retain that ACL, so use normal inheritance.
    return 0o777 if os.name == "nt" else 0o700


@contextmanager
def _result_staging_directory(parent: Path, prefix: str) -> Iterator[Path]:
    """Create a private POSIX staging dir with inheritable Windows ACLs."""
    directory = parent / f"{prefix}{uuid.uuid4().hex}"
    directory.mkdir(mode=_staging_mode())
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


@dataclass(frozen=True)
class ConversionOptions:
    pdf: Path
    output_root: Path
    token: str
    model: str = "vlm"
    language: str = "en"
    ocr: bool = False
    formula: bool = True
    table: bool = True
    pages: str | None = None
    timeout: int = 1800
    overwrite: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 MinerU 精准解析 API 转换本地 PDF，并保存 Markdown 解析结果。"
    )
    parser.add_argument("pdf", type=Path, help="要解析的本地 PDF 文件")
    parser.add_argument(
        "-o",
        "--output",
        dest="output_root",
        type=Path,
        default=Path("mineru_output"),
        help="结果根目录（默认：./mineru_output）",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="从指定的 .env 文件读取 MINERU_TOKEN（默认：读取当前运行目录下的 .env）",
    )
    parser.add_argument("--model", choices=("vlm", "pipeline"), default="vlm")
    parser.add_argument("--language", default="en")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--no-formula", action="store_true")
    parser.add_argument("--no-table", action="store_true")
    parser.add_argument("--pages")
    parser.add_argument("--timeout", type=positive_int, default=1800)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def _validate_common(options: ConversionOptions) -> tuple[Path, str]:
    if not isinstance(options.token, str) or not options.token.strip():
        raise ConversionError("未设置 MINERU_TOKEN")
    if options.model not in {"vlm", "pipeline"}:
        raise ConversionError("MinerU model 必须是 vlm 或 pipeline")
    if not isinstance(options.language, str) or not options.language.strip():
        raise ConversionError("MinerU language 必须是非空字符串")
    if (
        isinstance(options.timeout, bool)
        or not isinstance(options.timeout, int)
        or options.timeout <= 0
    ):
        raise ConversionError("MinerU timeout 必须是大于 0 的整数")

    requested = options.pdf.expanduser()
    try:
        pdf = requested.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConversionError(f"PDF 文件不存在或无法解析：{requested}") from error
    if not pdf.is_file():
        raise ConversionError(f"输入路径不是文件：{pdf}")
    if requested.suffix.lower() != ".pdf":
        raise ConversionError(f"当前程序只接受 PDF 文件：{requested}")
    try:
        size = pdf.stat().st_size
    except OSError as error:
        raise ConversionError(f"无法读取 PDF：{pdf}") from error
    if size > MAX_PDF_BYTES:
        raise ConversionError("PDF 超过 MinerU 精准解析 API 的 200 MB 限制")
    return pdf, requested.stem


def validate_options(options: ConversionOptions) -> tuple[Path, Path]:
    pdf, requested_stem = _validate_common(options)
    output_root = options.output_root.expanduser().resolve()
    destination = output_root / requested_stem
    if destination.exists() and not options.overwrite:
        raise ConversionError(f"结果目录已存在：{destination}。如需替换，请添加 --overwrite")
    if destination.exists() and not destination.is_dir():
        raise ConversionError(f"结果路径已存在且不是目录：{destination}")
    return pdf, destination


def _reject_result_symlinks(root: Path) -> None:
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ConversionError(f"MinerU 结果包含符号链接，拒绝发布：{entry.relative_to(root)}")
        if not entry.is_file() and not entry.is_dir():
            raise ConversionError(f"MinerU 结果包含特殊文件，拒绝发布：{entry.relative_to(root)}")


def organize_result(directory: Path) -> Path:
    """Remove returned PDF copies and move auxiliary artifacts into ``temp/``."""
    try:
        root = directory.resolve(strict=True)
    except OSError as error:
        raise ConversionError(f"解析结果路径不存在：{directory}") from error
    if not root.is_dir() or root.is_symlink():
        raise ConversionError(f"解析结果路径不是普通目录：{root}")
    _reject_result_symlinks(root)

    full_markdown = root / "full.md"
    if full_markdown.is_symlink() or not full_markdown.is_file():
        raise ConversionError("MinerU 结果缺少普通文件 full.md")

    for entry in list(root.iterdir()):
        if entry.is_file() and entry.suffix.lower() == ".pdf":
            entry.unlink()

    images_dir = root / "images"
    if images_dir.exists() and not images_dir.is_dir():
        raise ConversionError(f"无法整理结果：{images_dir} 不是目录")
    temp_dir = root / "temp"
    if temp_dir.exists() and not temp_dir.is_dir():
        raise ConversionError(f"无法整理结果：{temp_dir} 不是目录")

    def should_stay(entry: Path) -> bool:
        if entry.name in {"images", "temp"}:
            return True
        return entry.is_file() and entry.name == "full.md"

    to_move = [entry for entry in root.iterdir() if not should_stay(entry)]
    for entry in to_move:
        target = temp_dir / entry.name
        if target.exists() or target.is_symlink():
            raise ConversionError(f"无法整理结果，目标已存在：{target}")
    temp_dir.mkdir(exist_ok=True)
    for entry in to_move:
        entry.replace(temp_dir / entry.name)

    _reject_result_symlinks(root)
    return temp_dir


def _save_result(
    options: ConversionOptions,
    pdf: Path,
    staging: Path,
    client_factory: Callable[[str], Any] | None,
) -> None:
    if client_factory is None:
        try:
            from mineru import MinerU
        except ImportError as error:
            raise ConversionError("未安装 MinerU SDK") from error
        client_factory = MinerU

    print(f"正在提交：{pdf}")
    print(
        "解析参数："
        f"model={options.model}, language={options.language}, "
        f"ocr={options.ocr}, formula={options.formula}, table={options.table}"
    )
    try:
        with client_factory(options.token.strip()) as client:
            result = client.extract(
                str(pdf),
                model=options.model,
                ocr=options.ocr,
                formula=options.formula,
                table=options.table,
                language=options.language.strip(),
                pages=options.pages,
                timeout=options.timeout,
            )
            if getattr(result, "state", None) != "done":
                detail = getattr(result, "error", None) or "任务未成功完成"
                raise ConversionError(f"MinerU 解析失败：{detail}")
            result.save_all(str(staging))
            organize_result(staging)
    except ConversionError:
        raise
    except Exception as error:
        raise ConversionError(f"MinerU 请求或结果保存失败：{error}") from error


def convert_pdf(
    options: ConversionOptions,
    *,
    client_factory: Callable[[str], Any] | None = None,
) -> Path:
    """Convert one PDF and atomically publish a standalone result directory."""
    pdf, destination = validate_options(options)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with _result_staging_directory(
            destination.parent, f".{destination.name}-"
        ) as staging:
            _save_result(options, pdf, staging, client_factory)
            if destination.exists():
                shutil.rmtree(destination)
            staging.replace(destination)
    except ConversionError:
        raise
    except OSError as error:
        raise ConversionError(f"无法发布 MinerU 结果：{error}") from error
    return destination


def _rollback_published(published: list[tuple[Path, Path]]) -> None:
    for source, target in reversed(published):
        try:
            if not (target.exists() or target.is_symlink()):
                continue
            if source.exists() or source.is_symlink():
                if source.is_dir() and target.is_dir():
                    shutil.rmtree(target)
                elif source.is_file() and target.is_file() and source.samefile(target):
                    target.unlink()
            else:
                target.replace(source)
        except OSError:
            # Preserve the original exception. A failed best-effort rollback is
            # still visible in the paper directory for manual recovery.
            pass


def convert_pdf_into_paper(
    options: ConversionOptions,
    paper_folder: Path,
    *,
    client_factory: Callable[[str], Any] | None = None,
) -> Path:
    """Stage a conversion, then publish its contents directly into a paper folder."""
    if paper_folder.is_symlink() or not paper_folder.is_dir():
        raise ConversionError(f"论文目录不是普通目录：{paper_folder}")
    ensure_pipeline_destination_available(paper_folder)
    pdf, _ = _validate_common(options)

    try:
        with _result_staging_directory(paper_folder, ".pdf2md-") as staging:
            _save_result(options, pdf, staging, client_factory)
            ensure_pipeline_destination_available(paper_folder)
            entries = list(staging.iterdir())
            full_markdown = staging / "full.md"
            staged_temp = staging / "temp"
            destination_temp = paper_folder / "temp"
            ordered: list[tuple[Path, Path]] = [
                (entry, paper_folder / entry.name)
                for entry in entries
                if entry not in {full_markdown, staged_temp}
            ]
            if destination_temp.exists():
                ordered.extend(
                    (entry, destination_temp / entry.name)
                    for entry in staged_temp.iterdir()
                )
            else:
                ordered.append((staged_temp, destination_temp))
            ordered.append((full_markdown, paper_folder / "full.md"))

            for source, target in ordered:
                if target.exists() or target.is_symlink():
                    raise ConversionError(f"转换结果名称与论文目录已有内容冲突：{target}")

            published: list[tuple[Path, Path]] = []
            try:
                for source, target in ordered:
                    if target.exists() or target.is_symlink():
                        raise ConversionError(
                            f"转换结果名称与论文目录已有内容冲突：{target}"
                        )
                    if source.is_file():
                        os.link(source, target)
                        published.append((source, target))
                    else:
                        source.rename(target)
                        published.append((source, target))
            except BaseException:
                _rollback_published(published)
                raise
    except ConversionError:
        raise
    except (OSError, ValueError) as error:
        raise ConversionError(f"无法发布论文转换结果：{error}") from error
    return paper_folder / "full.md"


def ensure_pipeline_destination_available(paper_folder: Path) -> None:
    """Fail before an API call when a managed root output name is occupied."""
    for name in PIPELINE_OUTPUT_NAMES:
        target = paper_folder / name
        if target.exists() or target.is_symlink():
            raise ConversionError(f"转换结果名称与论文目录已有内容冲突：{target}")
    temp = paper_folder / "temp"
    if temp.is_symlink() or (temp.exists() and not temp.is_dir()):
        raise ConversionError(f"转换结果名称与论文目录已有内容冲突：{temp}")


def load_vault_token(vault: Path) -> str:
    """Load MINERU_TOKEN from the real environment, then the vault-local .env."""
    if "MINERU_TOKEN" in os.environ:
        token = os.environ["MINERU_TOKEN"]
        if token.strip():
            return token.strip()
        raise ConversionError("环境变量 MINERU_TOKEN 为空")

    marrow_directory = vault / ".papermarrow"
    if marrow_directory.is_symlink() or not marrow_directory.is_dir():
        raise ConversionError(f".papermarrow 不是普通目录：{marrow_directory}")
    env_file = marrow_directory / ".env"
    if env_file.is_symlink():
        raise ConversionError(f"拒绝读取符号链接 Token 文件：{env_file}")
    if env_file.exists() and not env_file.is_file():
        raise ConversionError(f"Token 路径不是普通文件：{env_file}")
    if env_file.is_file():
        try:
            from dotenv import dotenv_values

            token = dotenv_values(env_file).get("MINERU_TOKEN")
        except (ImportError, OSError, ValueError) as error:
            raise ConversionError(f"无法读取 Token 文件：{env_file}") from error
        if isinstance(token, str) and token.strip():
            return token.strip()
        raise ConversionError(f"Token 文件缺少非空 MINERU_TOKEN：{env_file}")
    raise ConversionError(
        f"未设置 MINERU_TOKEN；请设置环境变量或写入 {env_file}"
    )


def load_env_file(path: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise ConversionError("未安装 python-dotenv") from error
    load_dotenv(path, override=False)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.env_file is not None:
            env_file = args.env_file.expanduser().resolve()
            if not env_file.is_file():
                raise ConversionError(f".env 文件不存在：{env_file}")
        else:
            env_file = Path.cwd() / ".env"
            if not env_file.is_file():
                env_file = None
        if env_file is not None:
            load_env_file(env_file)

        options = ConversionOptions(
            pdf=args.pdf,
            output_root=args.output_root,
            token=os.environ.get("MINERU_TOKEN", ""),
            model=args.model,
            language=args.language,
            ocr=args.ocr,
            formula=not args.no_formula,
            table=not args.no_table,
            pages=args.pages,
            timeout=args.timeout,
            overwrite=args.overwrite,
        )
        destination = convert_pdf(options)
    except ConversionError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130

    print(f"解析完成，结果已保存到：{destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
