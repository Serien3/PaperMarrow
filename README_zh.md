# PaperMarrow

## Overview

PaperMarrow 是一个面向 Zotero 与 Obsidian 用户的文献管理工具。它可以将 Zotero Collection 中的论文元数据与本地 PDF 导入 Obsidian vault，并按需将 PDF 转换为适合 AI 辅助阅读的 Markdown 文件。导入过程不会上传任何文件；PDF 转换仅在用户明确选择后执行。vault 中生成的所有内容均为普通 Markdown 文件，可以在 Obsidian 或其他编辑器中继续编辑。

## Table of Contents

- [Overview](#overview)
- [Key Feature](#key-feature)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)

## Key Feature

- **增量导入**：只导入尚未入库的论文，不修改或覆盖已有内容
- **显式授权转换**：PDF 仅在用户确认后上传转换；查看状态不消耗额度
- **PDF 符号链接**：vault 中保存指向 Zotero 原文件的链接，不复制文件本身
- **中断恢复**：导入过程中断后，再次运行可继续完成，不产生重复条目
- **标准 Markdown 格式**：所有文件均为普通 Markdown，可在 Obsidian 或其他编辑器中编辑，不依赖本工具
- **并发安全**：多篇论文的转换互不冲突，转换期间仍可正常编辑 vault

## Requirements

- Python >= 3.11（已在 3.11–3.14 验证）
- mineru-open-sdk == 0.2.5 for 通过 MinerU 将 PDF 转为 Markdown
- prompt-toolkit >= 3.0.52, < 4 for `convert` 的交互式多选界面
- python-dotenv == 1.0.1 for 读取 vault 内的 `.papermarrow/.env`
- PyYAML >= 6, < 7 for 解析与回读 `meta.md` 的 YAML frontmatter

以上 Python 依赖会随 PaperMarrow 一并安装，无需手动处理。

以下内容需要提前准备：

- **操作系统**：在安装 Zotero 的 Windows 或 Linux 电脑上运行
- **Zotero 桌面版**：保持运行，并在设置中允许本机其他应用访问；仅支持个人库与本机回环地址
- **MinerU 账号与 API Token**：`convert` 需要
- **支持符号链接的文件系统**：Windows 请开启「开发者模式」；Linux 默认支持。程序依赖硬链接实现安全写入（NTFS 默认支持，FAT/exFAT 不支持），`doctor` 会实际检查
- **（可选）Obsidian**：vault 只存放普通 Markdown 与图片，不需要插件或额外 API

## Installation

推荐先创建虚拟环境再安装.

### Installation (Conda)

```bash
git clone https://github.com/Serien3/PaperMarrow.git
cd PaperMarrow
conda env create --name papermarrow_env --file environment.yml
conda activate papermarrow_env
pip install .
```

### Installation (venv)

Windows：

```powershell
git clone https://github.com/Serien3/PaperMarrow.git
cd PaperMarrow
py -3 -m venv .venv
.\.venv\Scripts\activate
pip install .
```

Linux：

```bash
git clone https://github.com/Serien3/PaperMarrow.git
cd PaperMarrow
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

此外，项目还附带一个 `paper-analysis-notes` skill，用于让 AI 助手精读论文并归纳出一份结构化的阅读笔记。可以将其复制到你的文献仓库中使用：

```bash
cp -r skills/paper-analysis-notes <你的文献仓库>/.agents/skills/
```

## Usage

以下示例均为 Windows PowerShell；Linux 下命令相同。不指定 `--vault` 时程序会自动识别所在的 vault；需要操作其他 vault 时可显式传入路径。

### 1. 配置

首次使用前需要初始化配置。在 Zotero 中选中要导入的 Collection，查询它的 8 位 key：

```powershell
papermarrow collections
```

然后在 vault 目录中初始化：

```powershell
papermarrow init --vault "D:\Research\PaperLibrary" --collection ABCDEFGH
```

`init` 的可用参数：

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--vault` | vault 路径 | 当前目录 |
| `--collection` | 要导入的 Zotero Collection 的 8 位 key | Required |
| `--zotero-url` | Zotero 本地 API 地址 | `http://localhost:23119/api/` |
| `--pdf2md-model` | 转换模型：`vlm` 或 `pipeline` | `vlm` |
| `--pdf2md-language` | 转换语言，如 `en`、`ch` | `en` |
| `--pdf2md-ocr` | 转换时启用 OCR | 关闭 |
| `--pdf2md-timeout` | 转换超时（秒） | `1800` |

`init` 之后，vault 中会生成一份 `.papermarrow/config.toml`，例如：

```toml
collection = "ABCDEFGH"
zotero_url = "http://localhost:23119/api/"

[pdf2md]
model = "vlm"
language = "en"
ocr = false
timeout = 1800
```

之后可以直接编辑这个文件修改设置。

要使用 `convert`（PDF 转 Markdown）功能，必须先配置 MinerU Token：写入环境变量 `MINERU_TOKEN`，或写入 `<vault>\.papermarrow\.env`：

```dotenv
MINERU_TOKEN='你的 Token'
```

环境变量优先。运行 `papermarrow doctor` 可以检查环境是否就绪（配置、Token、vault 可写性、符号链接能力、Zotero 连接）。

### 2. 导入论文

将 Zotero Collection 中的论文导入 vault，写入元数据并建立 PDF 链接：

```powershell
papermarrow sync
```

只导入尚未入库的论文，已入库的自动跳过；Zotero 已在本机保存 PDF 的论文会同时建立指向原文件的链接。命令可重复执行，用于导入新论文。

### 3. 修复 PDF 链接

Zotero 中 PDF 被移动、重命名，或首次导入时链接未建立成功，可补建或修复：

```powershell
papermarrow link-pdfs
```

扫描所有已入库论文，补建或修复缺失、失效的 PDF 链接。

### 4. 转换 PDF 为 Markdown

将 PDF 转换为适合 AI 阅读的 Markdown。直接运行会打开交互式多选界面：

```powershell
papermarrow convert
```

方向键移动、Space 选择、`/` 搜索、Enter 确认、`q` 取消；只有可转换的论文可以被选择，确认后按顺序转换。

在脚本或非交互终端中查看各论文的转换状态：

```powershell
papermarrow convert --list
```

或显式指定要转换的论文：

```powershell
papermarrow convert `
  --key ABCD1234 `
  --key EFGH5678
```

转换结果写入论文目录：`full.md` 为正文，图片在 `images/`，辅助文件在 `temp/`。已转换的论文自动跳过，已有文件不会被覆盖。

### 5. 让 AI Agent 帮你深入分析论文

论文转换完成后，可以使用项目附带的 `paper-analysis-notes` skill，让 AI 助手精读论文并归纳出一份结构化的阅读笔记。将 skill 复制到你的文献仓库后，支持 Agent Skills 格式的 AI 助手会自动加载它，例如：

> /paper-analysis-note 帮我阅读这篇论文
