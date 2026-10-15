# PaperMarrow

**English** | [中文](README_zh.md)

## Overview

PaperMarrow is a literature management tool for Zotero and Obsidian users. It imports the bibliographic metadata and local PDFs of a Zotero Collection into an Obsidian vault, and converts PDFs to Markdown for AI-assisted reading on demand. No files are uploaded during import; PDF conversion runs only after explicit user confirmation. All files created in the vault are plain Markdown, which can be further edited in Obsidian or any other editor.

## Table of Contents

- [Overview](#overview)
- [Key Feature](#key-feature)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Installation (Conda)](#installation-conda)
  - [Installation (venv)](#installation-venv)
- [Usage](#usage)
  - [1. Configuration](#1-configuration)
  - [2. Import papers](#2-import-papers)
  - [3. Repair PDF links](#3-repair-pdf-links)
  - [4. Convert PDFs to Markdown](#4-convert-pdfs-to-markdown)
  - [5. Deep analysis with your AI agent](#5-deep-analysis-with-your-ai-agent)

## Key Feature

- **Incremental import**: only imports papers that are not yet in the vault; existing content is never modified or overwritten
- **Explicit conversion**: PDFs are uploaded only after user confirmation; checking conversion status does not consume quota
- **PDF symlinks**: the vault stores links to the original PDFs in Zotero rather than copies of the files
- **Interrupted-import recovery**: if an import is interrupted, the next run resumes it without creating duplicate entries
- **Standard Markdown format**: all files are plain Markdown that can be edited in Obsidian or other editors, independent of this tool
- **Concurrency safety**: conversions of different papers do not conflict, and the vault remains editable during conversion

## Requirements

- Python >= 3.11 (tested on 3.11–3.14)
- mineru-open-sdk == 0.2.5 for PDF-to-Markdown conversion via MinerU
- prompt-toolkit >= 3.0.52, < 4 for the interactive `convert` selection TUI
- python-dotenv == 1.0.1 for loading the vault-local `.papermarrow/.env`
- PyYAML >= 6, < 7 for parsing and round-trip validation of `meta.md` YAML frontmatter

The Python dependencies above are installed automatically with PaperMarrow — no manual setup needed.

The following must be prepared beforehand:

- **Operating system**: Windows or Linux, on the machine where Zotero is installed
- **Zotero desktop app**: keep it running and allow other applications on this computer to access it; only the personal library and loopback addresses are supported
- **MinerU account and API token**: required for `convert`
- **A filesystem with symlink support**: on Windows enable Developer Mode; Linux supports symlinks by default. The program also relies on hard links for safe writes (supported by default on NTFS, not on FAT/exFAT). `doctor` actually checks symlink capability
- **(Optional) Obsidian**: the vault holds only plain Markdown and images; no plugin or extra API needed

## Installation

We recommend creating a virtual environment before installing.

### Installation (Conda)

```bash
git clone https://github.com/Serien3/PaperMarrow.git
cd PaperMarrow
conda env create --name papermarrow_env --file environment.yml
conda activate papermarrow_env
pip install .
```

### Installation (venv)

Windows:

```powershell
git clone https://github.com/Serien3/PaperMarrow.git
cd PaperMarrow
py -3 -m venv .venv
.\.venv\Scripts\activate
pip install .
```

Linux:

```bash
git clone https://github.com/Serien3/PaperMarrow.git
cd PaperMarrow
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

Additionally, the project ships a `paper-analysis-notes` skill that guides AI assistants through close reading of a paper and the writing of a structured reading note. Copy it into your literature repository to use it:

```bash
cp -r skills/paper-analysis-notes <your-literature-repo>/.agents/skills/
```

## Usage

All examples below are Windows PowerShell; the commands are identical on Linux. When `--vault` is omitted, the vault is detected automatically; pass the path explicitly to operate on a different vault.

### 1. Configuration

Before the first use, initialize the configuration. Select the target Collection in Zotero and look up its 8-character key:

```powershell
papermarrow collections
```

Then initialize the vault:

```powershell
papermarrow init --vault "D:\Research\PaperLibrary" --collection ABCDEFGH
```

Available `init` options:

| Option | Description | Default |
| --- | --- | --- |
| `--vault` | vault path | current directory |
| `--collection` | 8-character key of the Zotero Collection to import | Required |
| `--zotero-url` | Zotero Local API address | `http://localhost:23119/api/` |
| `--pdf2md-model` | conversion model: `vlm` or `pipeline` | `vlm` |
| `--pdf2md-language` | conversion language, e.g. `en`, `ch` | `en` |
| `--pdf2md-ocr` | enable OCR during conversion | off |
| `--pdf2md-timeout` | conversion timeout (seconds) | `1800` |

After `init`, a `.papermarrow/config.toml` is created in the vault, for example:

```toml
collection = "ABCDEFGH"
zotero_url = "http://localhost:23119/api/"

[pdf2md]
model = "vlm"
language = "en"
ocr = false
timeout = 1800
```

You can edit this file later to change the settings.

To use `convert` (PDF to Markdown), a MinerU token is required: put it in the `MINERU_TOKEN` environment variable or in `<vault>\.papermarrow\.env`:

```dotenv
MINERU_TOKEN='your token'
```

The environment variable takes precedence. Run `papermarrow doctor` to check whether the environment is ready (configuration, token, vault writability, symlink capability, Zotero connection).

### 2. Import papers

Import the papers of the Zotero Collection into the vault, writing metadata and building PDF links:

```powershell
papermarrow sync
```

Only papers not yet imported are processed; already-imported ones are skipped. Papers whose PDFs are stored locally by Zotero also get a link to the original file. Re-run the command whenever new papers arrive.

### 3. Repair PDF links

If PDFs were moved or renamed in Zotero, or links were not built on the first import:

```powershell
papermarrow link-pdfs
```

Scans all imported papers and builds or repairs missing and broken PDF links.

### 4. Convert PDFs to Markdown

Convert PDFs to Markdown suitable for AI-assisted reading. Running the command without arguments opens an interactive multi-select UI:

```powershell
papermarrow convert
```

Arrow keys move, Space selects, `/` searches, Enter confirms, `q` cancels; only papers that are ready can be selected, and confirmed papers are converted in order.

List the conversion status of all papers in scripts or non-interactive terminals:

```powershell
papermarrow convert --list
```

Or convert specific papers explicitly:

```powershell
papermarrow convert `
  --key ABCD1234 `
  --key EFGH5678
```

Results are written into the paper folder: `full.md` is the body text, images go to `images/`, auxiliary files to `temp/`. Already-converted papers are skipped, and existing files are never overwritten.

### 5. Deep analysis with your AI agent

After conversion, the bundled `paper-analysis-notes` skill lets an AI assistant read a paper closely and write a structured reading note. Once the skill is copied into your literature repository, AI assistants that support the Agent Skills format load it automatically; for example:

> /paper-analysis-note Read this paper for me
