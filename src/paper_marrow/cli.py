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
    result = argparse.ArgumentParser(description="Import Zotero Collection metadata into an Obsidian vault")
    commands = result.add_subparsers(dest="command", required=True)

    collections = commands.add_parser("collections", help="List Collections in the local Zotero library")
    collections.add_argument("--zotero-url", default=DEFAULT_ZOTERO_URL)

    init = commands.add_parser("init", help="Initialize minimal configuration in an existing or new vault")
    init.add_argument("--vault", type=Path, help="Vault path (default: current directory)")
    init.add_argument("--collection", required=True, help="8-character Zotero Collection key")
    init.add_argument("--zotero-url", default=DEFAULT_ZOTERO_URL)
    init.add_argument("--pdf2md-model", choices=("vlm", "pipeline"), default="vlm")
    init.add_argument("--pdf2md-language", default="en")
    init.add_argument("--pdf2md-ocr", action="store_true")
    init.add_argument("--pdf2md-timeout", type=int, default=1800)

    doctor = commands.add_parser("doctor", help="Check configuration, vault, and Zotero connection")
    doctor.add_argument("--vault", type=Path, help="Vault path (default: discovered upward from the current directory)")

    sync = commands.add_parser("sync", help="Import Zotero metadata for items not yet in the vault")
    sync.add_argument("--vault", type=Path, help="Vault path (default: discovered upward from the current directory)")
    link_pdfs = commands.add_parser("link-pdfs", help="Create or repair Zotero PDF links for imported papers")
    link_pdfs.add_argument(
        "--vault", type=Path, help="Vault path (default: discovered upward from the current directory)"
    )
    convert = commands.add_parser("convert", help="Interactively select, list, or convert imported papers")
    convert.add_argument("--vault", type=Path, help="Vault path (default: discovered upward from the current directory)")
    mode = convert.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="List conversion readiness status of all papers")
    mode.add_argument(
        "--key",
        action="append",
        dest="keys",
        metavar="ITEM_KEY",
        help="Convert the given Zotero item key; repeatable",
    )
    return result


def _vault(value: Path | None, *, for_init: bool = False) -> Path:
    """Resolve an explicit vault or discover the nearest initialized ancestor."""
    try:
        if value is not None:
            return value.expanduser().resolve()
        current = Path.cwd().resolve()
    except (OSError, RuntimeError) as error:
        raise PipelineError(f"cannot resolve vault path: {error}") from error

    if for_init:
        return current
    for candidate in (current, *current.parents):
        marker = candidate / ".papermarrow" / "config.toml"
        if marker.exists() or marker.is_symlink():
            return candidate
    raise PipelineError(
        f"cannot find .papermarrow/config.toml upward from the current directory: {current}; "
        "enter an initialized vault, or pass --vault explicitly"
    )


def doctor(vault: Path) -> int:
    errors: list[str] = []
    try:
        config = load_config(vault)
        print("OK: configuration")
    except PipelineError as error:
        print(f"Action needed: configuration — {error}")
        return 1
    try:
        load_vault_token(vault)
        print("OK: MinerU token (convert available)")
    except PipelineError as error:
        print(f"Note: MinerU token not ready, sync is unaffected — {error}")
    try:
        with tempfile.TemporaryFile(dir=vault) as handle:
            handle.write(b"check")
        print("OK: vault writable")
    except OSError as error:
        errors.append(str(error))
        print(f"Action needed: vault not writable — {error}")
    try:
        with tempfile.TemporaryDirectory(dir=vault) as directory:
            source = Path(directory) / "source.pdf"
            link = Path(directory) / "link.pdf"
            source.write_bytes(b"check")
            os.symlink(source, link, target_is_directory=False)
            if not link.is_symlink():
                raise OSError("result is not a symlink")
        print("OK: PDF symlinks")
    except OSError as error:
        errors.append(str(error))
        print(f"Action needed: PDF symlinks — {error} (on Windows enable Developer Mode)")
    try:
        Zotero(config.zotero_url).items(config.collection)
        print("OK: Zotero and Collection")
    except (PipelineError, OSError) as error:
        errors.append(str(error))
        print(f"Action needed: Zotero and Collection — {error}")
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
            print(f"Initialized: {vault}")
            return 0
        if args.command == "doctor":
            return doctor(vault)

        config = load_config(vault)
        if args.command == "convert":
            if not args.list and args.keys is None and not supports_tui():
                raise PipelineError(
                    "the current terminal does not support the interactive picker; "
                    "use convert --list or convert --key ITEM_KEY"
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
                print("Checking paper conversion status...", flush=True)
                candidates = service.list_candidates()
                if not candidates:
                    print("No imported papers.")
                    return 0
                selected = run_conversion_picker(candidates)
                if selected is None:
                    print("Canceled.", file=sys.stderr)
                    return 130
                args.keys = selected
            token = load_vault_token(vault)
            summary = service.convert_selected(args.keys, token)
            for message in summary.messages:
                print(message, file=sys.stderr)
            print(
                f"Conversion complete: selected={summary.selected} converted={summary.converted} "
                f"skipped={summary.skipped} failed={summary.failed}"
            )
            return 0 if summary.successful else 1
        if args.command == "link-pdfs":
            with VaultLock(vault):
                summary = PdfLinker(Zotero(config.zotero_url)).link_existing(vault)
            for message in summary.messages:
                print(message, file=sys.stderr)
            print(
                f"PDF links complete: papers={summary.papers} linked={summary.linked} "
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
            f"Sync complete: created={summary.created} skipped={summary.skipped} "
            f"conflicts={summary.conflicts} failed={summary.failed} "
            f"pdf_linked={summary.pdf_linked} pdf_missing={summary.pdf_missing} "
            f"pdf_failed={summary.pdf_failed}"
        )
        if summary.created_keys:
            print("Keys added this run: " + " ".join(summary.created_keys))
        return 0 if summary.successful else 1
    except (PipelineError, OSError, ValueError, TypeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
