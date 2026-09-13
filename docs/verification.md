# 评审回应与验证清单（0.2.0）

本文回应 2026-09-13 的代码评审（基线提交 `3396e77`），逐条给出处理结果、验证方式与仍未完成的部分。

## 0. 基线澄清

评审基于 `3396e77`。其中「凭据测试污染真实 macOS 钥匙串」一条已在 `6e12fbe` 修复
（提交时间早于评审），修复方式是：测试里把 DPAPI / 钥匙串 / Secret Service 三个后端全部替换为
不可用，并断言子进程不被调用。0.2.0 又补了一条回归测试
`test_module_has_no_top_level_wintypes_import` 与「禁止调用系统凭据命令」的守卫。

## 1. 第一阶段：基础问题

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 凭据测试隔离 | 已完成 | `tests/test_credentials.py` 在 `setUp` 里替换三个系统后端，并把 `subprocess.run` 替换为断言失败；任何测试都不得创建、修改、删除真实凭据 |
| `mubu_get_doc(format=json)` 截断 | 已完成 | 删除 60000 字符切片；`format=json` 返回完整 JSON，大文档走 `mubu_get_doc_json` 游标分页。回归测试 `test_get_doc_json_is_complete_and_parseable` 用 4000 个节点的文档断言 `json.loads` 成功且节点数不丢 |
| 全量离线测试 | 已完成 | 103 个用例，全部离线（网络访问被 mock） |
| 三平台 CI | 已完成 | GitHub Actions：ubuntu / macOS / windows × Python 3.10–3.13，外加 ruff |

## 2. 第二阶段：结构化只读数据

`mubu_list` 现在同时返回文字与 `structuredContent`，并声明 `outputSchema`。示例（真实字段名来自接口）：

```json
{
  "folderId": "0",
  "folderName": null,
  "folders": [
    {"id": "f1", "name": "工作", "folderId": "0", "updateTime": 1789301039119,
     "type": "folder", "parentId": "0", "order": null, "updatedAt": 1789301039119}
  ],
  "documents": [
    {"id": "d1", "name": "会议记录", "folderId": "0", "updateTime": 1789301039481,
     "seq": 2, "type": "document", "parentId": "0", "order": 2,
     "updatedAt": 1789301039481}
  ]
}
```

约定：底层字段**原样保留**（上例的 `folderId`、`updateTime`、`seq`），另外补 `type` / `parentId` /
`order` / `updatedAt` 四个规范化字段。`order` 依次尝试 `seq`、`index`、`sort`，取不到就是 `null`，
不猜数值。

文档侧 `mubu_get_doc_json` 返回：

```json
{
  "docId": "d1",
  "name": "会议记录",
  "baseVersion": 7,
  "metadata": {"name": "会议记录", "baseVersion": 7, "author": {"id": 1}},
  "totalTopLevelNodes": 3,
  "totalNodes": 12,
  "offset": 0,
  "limit": 20,
  "hasMore": true,
  "nextCursor": "20",
  "nodes": [ ... ]
}
```

`metadata` 是 `get_doc` 返回中除 `definition` 外的全部字段，不删减。

## 3. 第三阶段：图片、附件与链接

**尚未完成，需要你的测试文档。** 幕布的 `definition` 里图片/附件/链接使用什么字段名，
只能从真实文档里确认；我不能靠猜写进转换器——猜错会把正文渲染坏。

已经就位的工具（无需改代码即可用于调查）：

```bash
mubu-web-mcp inspect <文档id>          # 人可读摘要 + 完整 JSON
mubu-web-mcp inspect <文档id> --json   # 只要 JSON
```

它输出的是**脱敏结构报告**：字段名及出现次数、顶层/总节点数、疑似 URL 字段（只保留主机名，
如 `<url host=assets.mubu.com>`）、以及把正文替换成类型占位的样例。文档正文与备注不会出现在
结果里，可以安全地贴给别人。

需要你提供（或授权我自己挑）以下文档的 id：

1. 含多张图片的文档
2. 含普通网页链接的文档
3. 含幕布内部文档链接的文档
4. 含备注/表格/公式的文档

拿到报告后，我会确认图片字段名、图片地址域名与是否过期、下载所需请求头、链接字段，
再补 `mubu_markdown` 的图片与链接渲染。

图片下载的安全边界**已经先写好**（`backup.py`）：只允许 `mubu.com` 及其子域、重定向后二次校验、
非幕布域名绝不附带 JWT；外部链接一律不发请求。测试覆盖了 `notmubu.com`、`mubu.com.evil.com`
这类仿冒域名。

## 4. 第四阶段：限流与稳定性

| 要求 | 实现 |
| --- | --- |
| `Retry-After` | HTTP 429 与带 `Retry-After` 的 503 都会遵守，秒数与 HTTP 日期都支持，上限 `MUBU_MAX_BACKOFF_SECONDS`（默认 60） |
| 429 / 5xx / 网络 / 认证分开 | `RateLimitError` / `MubuError` / `MubuError(网络)` / `AuthError` 四类，互不混淆 |
| 指数退避 + 抖动 | `2^attempt + random(0, jitter)`，抖动默认 150ms |
| 认证失败不循环 | 重登**一次**后仍失败即抛出；有测试断言调用次数不增长 |
| HTTP 200 内的业务限流码 | 可配置 `MUBU_RATE_LIMIT_CODES`，并内置「频繁 / 过快 / 限流 / too many / rate limit」等文案识别 |
| 跨进程限速 | `~/.mubu/rate.lock`（文件锁）+ `~/.mubu/rate.stamp`（共享时间戳），持锁期间睡眠，保证多 Agent 不叠加请求 |
| 并发登录 | 登录前取 `~/.mubu/login.lock`，避免多进程同时刷新 token |
| 取消 | `notifications/cancelled` → 置位事件 → 中止请求与退避等待；`serve()` 改为工作线程处理请求，取消才可能被收到 |
| 日志不含正文与凭据 | 诊断只记计数与错误码；`backup` 的进度日志只有路径 |
| 测试覆盖 | 429 + Retry-After、连续 5xx、断网重试耗尽、认证过期、业务限流码、退避上限、取消（请求前/等待中/重试中）、跨进程时间戳等待 |

默认间隔从 200ms 提到 500ms；备份引擎默认 2 秒（`--interval-ms` 可调）。

## 5. 第五阶段：本地备份库

`src/mubu_web_mcp/backup.py` 直接复用 `MubuClient` 与 `mubu_markdown`，在本机完成读取与写盘：

```bash
mubu-web-mcp backup --out ~/mubu-backup
```

* 递归索引，`--depth` / `--max-folders` / `--max-docs` 三重上限，超限即停并记录原因；
* 增量：`updateTime` 未变且文件仍存在 → 跳过，**不调用 `get_doc`**（有测试断言调用列表为空）；
* 断点续传：每处理完一个目录写 `.backup-state.json`，重跑读取剩余队列，成功结束后删除；
* 输出 `<序号>-<安全文件名>.md` 与 `manifest.json`（大小、SHA-256、baseVersion、资产列表）；
* 退出码 130 表示被中断，并提示可从断点继续；
* 只读保证：测试断言引擎不触碰任何写接口。

## 6. 已验证 / 尚未验证

**已验证**

* 登录、列目录、读文档、搜索（Windows 真机 + 你自己的账号）
* `Markdown → import_doc → 读回` 逐字节一致（含缩进、勾选、备注）
* MCP 协议：`initialize` / `tools/list` / `tools/call` / 取消通知，stdio 全程
* 结构化输出与游标分页（单元测试）
* 备份引擎的索引、增量、续传、dry-run（单元测试 + 真机 dry-run）
* CI：三系统 × 四 Python 版本 + ruff

**尚未验证**

* 图片 / 附件 / 链接字段（缺测试文档，见第 3 节）
* 表格、公式、标签、截止日期、高亮等节点字段的渲染
* 有序列表编号、折叠状态、多行备注的保真
* macOS / Linux 上的真实账号登录（CI 只能保证 import 与离线测试通过）
* 备份引擎在超大账号（上千文档）下的表现与限流实际触发情况
* 幕布侧改版导致接口变化的情况

## 7. 与旧版客户端的兼容性

* **工具**：`mubu_whoami` / `mubu_list` / `mubu_get_doc` / `mubu_search` /
  `mubu_create_doc` / `mubu_create_folder` 名称与参数不变，文字输出格式不变；
  新增 `structuredContent` 与 `outputSchema` 属于增量字段，旧客户端忽略即可。
* **`mubu_get_doc(format="json")`**：以前返回被截断的字符串，现在返回完整 JSON。
  解析逻辑正常的客户端只会变得更好；如果谁依赖「最多 60000 字符」，需要改用
  `mubu_get_doc_json` 分页。
* **凭据文件**：仍然读取旧的 `~/.mubu/credentials.json`；新的写入位置是
  `~/.mubu/secrets.json`（明文回退）或系统安全存储。`~/.mubu/token.json` 同样兼容读取，
  新写入优先走系统安全存储。
* **环境变量**：`MUBU_MIN_INTERVAL_MS` 默认值从 200 变为 500（更保守）；
  新增 `MUBU_JITTER_MS`、`MUBU_MAX_BACKOFF_SECONDS`、`MUBU_PROCESS_LOCK`、
  `MUBU_RATE_LIMIT_CODES`，都有默认值，不设置也能跑。
* **CLI**：原有子命令不变，`--version` 输出 `0.2.0`。
