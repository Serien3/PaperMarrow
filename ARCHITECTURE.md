# 项目架构

本文面向首次修改代码的贡献者，描述当前实现。安装与命令用法见 [README.md](README.md)；实现位于 `src/paper_marrow/`，下文模块路径均相对此目录。

## 整体视图

PaperMarrow 将 Zotero 个人库中一个 Collection 的直接成员一次性导入用户的 Obsidian vault，再由用户明确选择是否上传 PDF 至 MinerU。它是**按命令运行的模块化单体 CLI**：应用逻辑、终端选择器与文件写入在同一个 Python 进程中，没有后台服务、任务队列或数据库。仅 `sync` 的 PDF 链接阶段使用线程池。

运行时涉及三个外部部分：

- **Zotero 桌面进程**：通过只读 Local API 提供元数据、附件列表和本机文件地址；当前只支持回环 HTTP 地址及个人库 `users/0`。
- **本机文件系统**：vault 是持久状态来源；PDF 仍由 Zotero 存储，vault 只持有符号链接。Obsidian 直接读取这些文件，不需要插件或进程间 API。
- **MinerU 远程服务**：只有显式转换才通过 SDK 上传 PDF、等待结果并下载产物。应用不自行实现解析模型。

```text
CLI（cli.main）
 ├─ sync ─────── Importer ─── Zotero Local API
 │                 ├─ JSON 快照 → meta.md
 │                 └─ PdfLinker → 本机 PDF 符号链接
 ├─ link-pdfs ── PdfLinker ── Zotero Local API → 修复既有链接
 └─ convert ──── ConversionService → 本地候选状态
                    ↑ 用户授权：TUI 返回 keys / --key
                    └─ pdf2md → MinerU SDK → 暂存 → 论文目录
```

`sync` 与转换的分离是额度和数据上传边界，不只是命令拆分：`Importer` 不调用 MinerU；`convert` 不访问 Zotero，只使用 vault 当前链接的 PDF。旧配置的 `pdf2md.enabled` 仅兼容读取，不能恢复自动转换。

## 代码地图

### 入口与交互

- **`cli.py` — `parser`、`main`、`_vault`**：组装依赖、发现 vault、分派命令、控制锁的生命周期并输出汇总。`pyproject.toml` 的 `papermarrow` 和 `__main__.py` 都进入 `cli.main`。显式 `--vault` 优先，否则向上查找最近的 `.papermarrow/config.toml`；`init` 默认使用当前目录。
- **`conversion_tui.py` — `PickerModel`、`run_conversion_picker`**：`PickerModel` 持有搜索、光标、选择和确认状态，独立于 prompt-toolkit 渲染。选择器只接收 `ConversionCandidate` 并返回已确认的 key 列表或 `None`，不读取 Token、不上传 PDF。默认空选，仅 `Ready` 可选；确认后先退出界面，再由 CLI 执行转换。

CLI 是授权边界：裸 `convert` 要求 stdin/stdout 都是终端；`--list` 仅检查状态，`--key` 构成非交互授权。Token 在 TUI 确认后才加载，因此浏览和取消不需要凭据。

### 配置、身份与文件格式

- **`library.py` — `Config`、`Pdf2MdConfig`、`initialize`、`load_config`**：创建和严格校验 vault 配置。初始化只新增配置，不生成 Obsidian 首页、模板或其他用户内容。
- **`library.py` — `index_existing`、`ExistingIndex`**：仅扫描 vault 的直接子目录，通过文件中的 Zotero 原生 `key` 建立内存索引；不以目录名或 DOI 判定身份。`complete` 来自 `meta.md`，`partial` 来自缺少 `meta.md` 时的 `temp/zotero-item.json`；同一 key 的多个声明是冲突。
- **`files.py` — `project_meta`、`render_meta`、`render_zotero_json`、`paper_folder_name`**：集中处理元数据投影、序列化与目录命名。JSON 保留 `item.data` 的完整值；YAML 仅把 creators 转成人名列表、tags 转为 `zoteroTags`，不增添 PaperMarrow 状态字段。序列化会回读校验值和类型。

磁盘布局同时承担索引与恢复协议：

```text
<vault>/
  .papermarrow/
    config.toml                  # Collection、Local API、转换默认参数
    .env                         # 可选 MINERU_TOKEN
    writer.lock                  # vault 写锁的载体
    pdf2md-locks/<key>.lock       # 单篇转换锁的载体
  <用户可重命名的论文目录>/
    meta.md                      # 入库完成标志；原生 key 用于索引
    <附件名> [<attachment-key>].pdf -> Zotero 本机 PDF
    full.md                      # 普通文件存在即视为 Converted
    images/                      # 可选转换图片
    temp/
      zotero-item.json           # 首次导入的原始快照
      ...                        # MinerU 辅助产物
```

不保存单独的任务状态表。目录在 vault 根层内重命名仍可识别，移入更深层级则不再被索引。`meta.md` 存在时不再读取 sidecar；既有元数据损坏会使索引失败，而不是静默漏过并重复导入。根目录旧式 `zotero-item.json` 不参与索引或自动迁移。

### 导入与附件链接

- **`ingest.py` — `Importer.sync`、`ImportSummary`**：读取 Collection、校验条目、排除附件/笔记/批注及冲突 key，串行创建元数据，再为本轮新建条目链接 PDF。
- **`pdf_links.py` — `PdfLinker`、`PdfAttachment`、`ensure_pdf_link`**：识别 PDF 附件、转换 `file:` URI、创建或修复符号链接。附件身份依靠链接名称末尾的 attachment key；可更新受管理链接的名称和目标，但普通文件及用户目录发生名称冲突时保留原内容。多个 PDF 全部链接，不猜测正文；附件在 Zotero 中删除不会触发本地清理。
- **`zotero.py` — `Zotero`**：封装只读 HTTP、分页与响应校验，禁用代理。通过 `Zotero-Server-Id` 检查数据库身份，通过分页返回的版本头检查列表读取期间是否变化；这些检查不是整个导入过程的数据库事务。`new_session` 为链接任务创建独立 opener，并继承已知数据库身份。

一次典型导入按以下顺序推进：

1. CLI 持有 `VaultLock`，`Importer` 读取远端列表和本地索引，跳过已完成 key。
2. 对新 key，先完成 JSON/YAML 渲染校验，再创建 `temp/zotero-item.json`，最后创建 `meta.md`。
3. 如果上次中断只留下 JSON，本轮再次遇到该 key 时从原目录的快照恢复 `meta.md`，不改用当前 Zotero 数据或新标题目录。
4. 元数据阶段结束后，仅为成功新增论文启动链接任务；链接失败进入汇总，不回滚元数据。已有论文需通过 `link-pdfs` 补建或修复。

**所有权边界**：`meta.md` 创建后本地内容归用户；再次 `sync` 不更新、不删除，也不补写缺少的 sidecar。这优先保护用户修改，而不是维持 Zotero 与 vault 的持续镜像。

### 转换编排与 MinerU 适配

- **`conversion.py` — `ConversionService`、`ConversionCandidate`、`ConversionSummary`**：从本地索引推导 `Ready` / `Converted` / `Unavailable`。只有恰好一个有效的受管理 PDF 符号链接且输出位置可用才为 `Ready`；普通 PDF 文件不参与选择。`convert_selected` 校验并去重 keys，按请求顺序串行处理；TUI 返回界面排序后的顺序。
- **`pdf2md.py` — `ConversionOptions`、`convert_pdf_into_paper`**：检查输入及输出条件，调用 SDK，整理并发布结果。`_save_result` 是 SDK 集成点，通过可注入的 `client_factory` 隔离真实网络；`organize_result` 验证普通 `full.md`，拒绝结果内符号链接及特殊文件，删除顶层 PDF 副本，把辅助文件归入 `temp/`。

候选列表只是快照。真正执行时，服务为该 key 取得 `PdfConversionLock` 并重新检查，再构建 `ConversionOptions`。上传前已知的输出冲突会阻止调用；下载结果中的辅助文件名事先未知，其与既有 `temp/` 内容的冲突只能在下载后检查，不能承诺所有失败都不消耗额度。

结果在论文目录内隐藏暂存目录中下载、整理。发布前再次检查目标，合并不重名的 `temp/` 条目，**最后发布 `full.md`**，使其成为完成标志。转换不覆盖已有产物；修改发布逻辑时须保留此保护协议。

## 横切机制与取舍

### 安全写入、锁与并发

- `files.atomic_create` 先写同目录临时文件并 `fsync`，再用硬链接实现“不存在才创建”，避免覆盖用户文件。元数据的原子性是**单文件级**，JSON 与 Markdown 不是整体事务；sidecar 恢复协议负责处理两次发布之间的中断。文件系统需支持硬链接。
- `VaultLock` 使用 Windows `msvcrt` / POSIX `flock` 的非阻塞操作系统锁；进程退出会释放锁，锁文件残留不代表仍在运行。CLI 包住 `init`、`sync`、`link-pdfs`；直接复用 `Importer` 或 `PdfLinker` 时调用者须安排写锁。
- `ConversionService` 仅在索引期间持有 vault 锁，不在 TUI 等待或远程转换期间占用它；实际转换持有 `.papermarrow/pdf2md-locks/<key>.lock`。不同 key 可由不同进程分别转换，同一命令内部仍串行。锁只协调遵循协议的程序，不锁定用户、Zotero 或 Obsidian 的编辑，也不让 PDF 链接刷新与转换成为一个整体事务。
- `sync` 的链接任务最多四路并发，元数据写入不并发。每个任务有独立 Zotero session；主线程按原条目顺序合并结果，保证汇总顺序稳定。中断时取消未开始任务并等待运行中线程结束，并非强制终止网络请求。
- 转换发布涉及多个文件：文件用硬链接发布，目录用重命名，失败时 `_rollback_published` 尽力撤销本次发布。它不是抗崩溃的多文件事务，回滚失败可能留下需手工处理的内容。不要把“先暂存、后发布”理解为整个论文目录一次原子替换。

符号链接避免复制 PDF、支持跨盘，但移动 Zotero 数据目录或跨机器同步 vault 会使链接失效；经链接修改 PDF 也会修改 Zotero 原文件。转换暂存目录在 POSIX 上使用私有权限，在 Windows 上使用通常的 ACL 继承，以免发布产物仍带私有 ACL。

### 错误、凭据与重试

预期业务错误使用 `files.PipelineError`，转换错误使用其子类 `ConversionError`。条目级错误进入各服务的 summary，单篇失败通常不阻止后续条目；配置、读取整体索引等全局错误交给 CLI 终止。CLI 通常以 0 表示成功、1 表示失败、130 表示取消/中断；进度与诊断写 stderr，命令汇总写 stdout。

`load_vault_token` 优先读取真实环境变量，其次读取 `<vault>/.papermarrow/.env`，不把 Token 写进配置。转换编排不自动重试：重跑须重新显式选择，以免意外重复上传或消费额度。

## 修改入口与验证

| 想修改什么 | 先读哪里 | 对应测试 |
| --- | --- | --- |
| 元数据属性、目录命名、断点恢复 | `files.py` → `library.index_existing` → `Importer.sync` | `ImportTests`、`ConfigAndFileTests` |
| PDF 附件识别、链接修复或并发 | `pdf_links.py` → `Zotero` → `Importer._link_created_papers` | `PdfLinkTests`、`AdapterAndCliTests` |
| 转换资格或状态原因 | `ConversionService.inspect` / `_local_pdf` → `pdf2md.ensure_pipeline_destination_available` | `ConversionServiceTests`、`Pdf2MdTests` |
| 选择、搜索、确认体验 | `PickerModel` → `run_conversion_picker` → `cli.main` | `tests/test_conversion_tui.py`、`AdapterAndCliTests` |
| MinerU 参数或产物布局 | `Pdf2MdConfig` → `ConversionOptions` → `_save_result` / `organize_result` | `ConfigAndFileTests`、`Pdf2MdTests` |

除单列的 TUI 测试外，表中测试类都在 `tests/test_pipeline.py`。例如调整 `temp/` 发布规则时，应同时查看索引如何识别 sidecar、导入如何恢复，以及转换如何合并和回滚，不能只改 SDK 结果整理函数。

测试使用临时目录、`FakeZotero`、`FakeMinerUClient`、可注入 client factory 和 mock 验证流程；TUI 使用管道输入与 `DummyOutput`，无需人工操作终端。安装包后执行：

```bash
.venv/bin/python -m pytest -q
```

这些测试不验证真实 Zotero / MinerU 服务或全部 Windows 文件系统行为；涉及真实服务协议、权限及符号链接能力的改动仍需在目标环境验证。
