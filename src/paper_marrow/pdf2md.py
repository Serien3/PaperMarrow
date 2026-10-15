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
        raise argparse.ArgumentTypeError("must be a positive integer")
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
        description="Convert a local PDF with the MinerU precision parsing API and save the Markdown result."
    )
    parser.add_argument("pdf", type=Path, help="local PDF file to parse")
    parser.add_argument(
        "-o",
        "--output",
        dest="output_root",
        type=Path,
        default=Path("mineru_output"),
        help="result root directory (default: ./mineru_output)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="read MINERU_TOKEN from the given .env file (default: .env in the current directory)",
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
        raise ConversionError("MINERU_TOKEN is not set")
    if options.model not in {"vlm", "pipeline"}:
        raise ConversionError("MinerU model must be vlm or pipeline")
    if not isinstance(options.language, str) or not options.language.strip():
        raise ConversionError("MinerU language must be a non-empty string")
    if (
        isinstance(options.timeout, bool)
        or not isinstance(options.timeout, int)
        or options.timeout <= 0
    ):
        raise ConversionError("MinerU timeout must be a positive integer")

    requested = options.pdf.expanduser()
    try:
        pdf = requested.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConversionError(f"PDF file does not exist or cannot be resolved: {requested}") from error
    if not pdf.is_file():
        raise ConversionError(f"input path is not a file: {pdf}")
    if requested.suffix.lower() != ".pdf":
        raise ConversionError(f"only PDF files are accepted: {requested}")
    try:
        size = pdf.stat().st_size
    except OSError as error:
        raise ConversionError(f"cannot read PDF: {pdf}") from error
    if size > MAX_PDF_BYTES:
        raise ConversionError("PDF exceeds the MinerU precision parsing API 200 MB limit")
    return pdf, requested.stem


def validate_options(options: ConversionOptions) -> tuple[Path, Path]:
    pdf, requested_stem = _validate_common(options)
    output_root = options.output_root.expanduser().resolve()
    destination = output_root / requested_stem
    if destination.exists() and not options.overwrite:
        raise ConversionError(f"result directory already exists: {destination}; add --overwrite to replace it")
    if destination.exists() and not destination.is_dir():
        raise ConversionError(f"result path already exists and is not a directory: {destination}")
    return pdf, destination


def _reject_result_symlinks(root: Path) -> None:
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ConversionError(f"MinerU result contains a symlink; refusing to publish: {entry.relative_to(root)}")
        if not entry.is_file() and not entry.is_dir():
            raise ConversionError(f"MinerU result contains a special file; refusing to publish: {entry.relative_to(root)}")


def organize_result(directory: Path) -> Path:
    """Remove returned PDF copies and move auxiliary artifacts into ``temp/``."""
    try:
        root = directory.resolve(strict=True)
    except OSError as error:
        raise ConversionError(f"parse result path does not exist: {directory}") from error
    if not root.is_dir() or root.is_symlink():
        raise ConversionError(f"parse result path is not a regular directory: {root}")
    _reject_result_symlinks(root)

    full_markdown = root / "full.md"
    if full_markdown.is_symlink() or not full_markdown.is_file():
        raise ConversionError("MinerU result lacks a regular full.md file")

    for entry in list(root.iterdir()):
        if entry.is_file() and entry.suffix.lower() == ".pdf":
            entry.unlink()

    images_dir = root / "images"
    if images_dir.exists() and not images_dir.is_dir():
        raise ConversionError(f"cannot organize result: {images_dir} is not a directory")
    temp_dir = root / "temp"
    if temp_dir.exists() and not temp_dir.is_dir():
        raise ConversionError(f"cannot organize result: {temp_dir} is not a directory")

    def should_stay(entry: Path) -> bool:
        if entry.name in {"images", "temp"}:
            return True
        return entry.is_file() and entry.name == "full.md"

    to_move = [entry for entry in root.iterdir() if not should_stay(entry)]
    for entry in to_move:
        target = temp_dir / entry.name
        if target.exists() or target.is_symlink():
            raise ConversionError(f"cannot organize result, target already exists: {target}")
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
            raise ConversionError("MinerU SDK is not installed") from error
        client_factory = MinerU

    print(f"Submitting: {pdf}")
    print(
        "Parse options: "
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
                detail = getattr(result, "error", None) or "task did not complete successfully"
                raise ConversionError(f"MinerU parsing failed: {detail}")
            result.save_all(str(staging))
            organize_result(staging)
    except ConversionError:
        raise
    except Exception as error:
        raise ConversionError(f"MinerU request or result save failed: {error}") from error


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
        raise ConversionError(f"cannot publish MinerU result: {error}") from error
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
        raise ConversionError(f"paper directory is not a regular directory: {paper_folder}")
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
                    raise ConversionError(f"conversion result name conflicts with existing content in the paper directory: {target}")

            published: list[tuple[Path, Path]] = []
            try:
                for source, target in ordered:
                    if target.exists() or target.is_symlink():
                        raise ConversionError(
                            f"conversion result name conflicts with existing content in the paper directory: {target}"
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
        raise ConversionError(f"cannot publish paper conversion result: {error}") from error
    return paper_folder / "full.md"


def ensure_pipeline_destination_available(paper_folder: Path) -> None:
    """Fail before an API call when a managed root output name is occupied."""
    for name in PIPELINE_OUTPUT_NAMES:
        target = paper_folder / name
        if target.exists() or target.is_symlink():
            raise ConversionError(f"conversion result name conflicts with existing content in the paper directory: {target}")
    temp = paper_folder / "temp"
    if temp.is_symlink() or (temp.exists() and not temp.is_dir()):
        raise ConversionError(f"conversion result name conflicts with existing content in the paper directory: {temp}")


def load_vault_token(vault: Path) -> str:
    """Load MINERU_TOKEN from the real environment, then the vault-local .env."""
    if "MINERU_TOKEN" in os.environ:
        token = os.environ["MINERU_TOKEN"]
        if token.strip():
            return token.strip()
        raise ConversionError("environment variable MINERU_TOKEN is empty")

    marrow_directory = vault / ".papermarrow"
    if marrow_directory.is_symlink() or not marrow_directory.is_dir():
        raise ConversionError(f".papermarrow is not a regular directory: {marrow_directory}")
    env_file = marrow_directory / ".env"
    if env_file.is_symlink():
        raise ConversionError(f"refusing to read symlinked token file: {env_file}")
    if env_file.exists() and not env_file.is_file():
        raise ConversionError(f"token path is not a regular file: {env_file}")
    if env_file.is_file():
        try:
            from dotenv import dotenv_values

            token = dotenv_values(env_file).get("MINERU_TOKEN")
        except (ImportError, OSError, ValueError) as error:
            raise ConversionError(f"cannot read token file: {env_file}") from error
        if isinstance(token, str) and token.strip():
            return token.strip()
        raise ConversionError(f"token file lacks a non-empty MINERU_TOKEN: {env_file}")
    raise ConversionError(
        f"MINERU_TOKEN is not set; set the environment variable or write it to {env_file}"
    )


def load_env_file(path: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise ConversionError("python-dotenv is not installed") from error
    load_dotenv(path, override=False)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.env_file is not None:
            env_file = args.env_file.expanduser().resolve()
            if not env_file.is_file():
                raise ConversionError(f".env file does not exist: {env_file}")
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
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCanceled.", file=sys.stderr)
        return 130

    print(f"Parsing done; result saved to: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
