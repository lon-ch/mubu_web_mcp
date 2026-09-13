# mubu_web_mcp

> 让你的 AI 智能体（Codex、Claude、Cursor、Windsurf…）通过 MCP 读写你的[幕布](https://mubu.com)大纲。

由 **miaoteam** 维护。English: [README.md](README.md)

---

## ⚠️ 先看这一段

* **这是非官方集成。** 与深圳市十里湖科技有限公司（幕布）没有任何隶属、授权或合作关系。
* 它直接调用**幕布网页端自己用的接口**（`api2.mubu.com/v3/api`），用**你自己的幕布手机号和密码**登录，不走 OAuth，也没有官方开发者计划。
* [幕布服务条款](https://mubu.com/agreement)明确禁止"通过非十里湖科技开发、授权的第三方软件、插件、外挂、系统，登录或使用软件及服务"。因此使用本项目**风险自负**，平台有权限流、暂停甚至终止相关账号。
* 实际情况是：它面向的是**低频、只操作自己账号数据**的个人使用。不要拿它做批量导出、爬取或任何近似滥用的行为。
* 也正因为如此，本项目**刻意不提供破坏性能力**：没有删除、重命名、移动、覆盖已有文档的工具，唯一的写操作是**新建**文档。

如果你不能接受这些，请改用[官方 MCP 集成](https://mubu.com/help/124)（需要付费会员）。

---

## 能做什么

接上之后，AI 在对话里就能直接调用这些工具：

| 工具 | 作用 |
| --- | --- |
| `mubu_whoami` | 查看当前账号和登录状态 |
| `mubu_list` | 列出某个目录下的文件夹和文档 |
| `mubu_get_doc` | 读文档，输出 Markdown 大纲（也可输出原始 JSON） |
| `mubu_search` | 按名称搜索文件夹和文档，可选搜正文 |
| `mubu_create_doc` | 用 Markdown **新建**文档（支持标题、缩进列表、`- [x]` 勾选、`> 备注`） |
| `mubu_create_folder` | 新建文件夹 |

典型用法："把刚才讨论的结论整理成大纲存到幕布"、"找一下我之前记的那篇关于 XX 的笔记"。

Markdown 与幕布大纲的转换是往返稳定的：写进去再读回来，标题、层级、勾选、备注逐字节一致。

---

## 快速开始

```bash
# 1. 安装（零第三方依赖）
pipx install git+https://github.com/miaoteam/mubu_web_mcp

# 2. 登录（会提示输入手机号和密码）
mubu-web-mcp login

# 3. 自动探测本机 AI 客户端并写入 MCP 配置，然后验证
mubu-web-mcp setup

# 4. 之后重启客户端即可（Codex / Claude Code 需新开会话）
mubu-web-mcp doctor
```

---

## 让智能体自己完成配置

把下面这段丢给你的智能体，它就能自己装好并接上：

```text
请帮我安装并接入这个 MCP 服务：https://github.com/miaoteam/mubu_web_mcp
步骤：(1) pipx install git+https://github.com/miaoteam/mubu_web_mcp
(2) 让我自己在终端执行 mubu-web-mcp login —— 不要向我索要密码，也不要把密码写进命令行或配置文件
(3) mubu-web-mcp setup --agent all --yes
(4) mubu-web-mcp doctor，并把结果给我看
细节参考 https://github.com/miaoteam/mubu_web_mcp/blob/main/AGENTS.md
```

面向智能体的完整说明见 [AGENTS.md](AGENTS.md)，各客户端的分步提示词见
[docs/agent-setup.md](docs/agent-setup.md)。

---

## 支持的客户端

`mubu-web-mcp install --agent <名称>` 会写入对应配置：

| 客户端 | 写入的文件 |
| --- | --- |
| `codex` | `~/.codex/config.toml` |
| `claude-desktop` | `claude_desktop_config.json`（按系统选择路径） |
| `claude-code` | 通过 `claude mcp add` 注册 |
| `cursor` | `~/.cursor/mcp.json` |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` |
| `vscode` | 当前项目的 `.vscode/mcp.json` |
| `cherry-studio` | 打印 JSON，手动粘进 GUI |

修改前会备份成 `<文件>.bak`，重复执行只会更新 `mubu_web_mcp` 这一项。`--dry-run` 只预览不落盘。

---

## 只读模式

只想让 AI 读、不让它写：

```bash
mubu-web-mcp install --agent codex --read-only
# 或者把 MUBU_READ_ONLY=1 写进服务端环境变量
```

只读模式下，创建类工具根本不会出现在工具列表里。

---

## 安全

**凭据不会离开你的电脑**，唯一的例外是登录请求本身要发给幕布。

* 按平台自动选择存储方式：
  * **Windows**：DPAPI 加密文件 `~/.mubu/credentials.dpapi`（密钥绑定当前 Windows 用户）
  * **macOS**：系统钥匙串
  * **Linux**：通过 `secret-tool` 存进 Secret Service
  * **兜底**：`~/.mubu/credentials.json`，权限 0600，并且 CLI 会明确告诉你降级了
* 登录 token 缓存在 `~/.mubu/token.json`（约 2 小时，自动续期）
* 代码里的唯一网络目标是 `https://api2.mubu.com/v3/api`，主机名硬编码，每次请求前再校验一次；没有遥测，没有第三方日志
* `mubu-web-mcp logout` 会删掉本机保存的一切

需要明白的是：**云端 AI 会把它读到的内容发给模型服务商**。敏感文档不要交给 AI 读，或者直接用库自己在本地写脚本。

完整威胁模型见 [SECURITY.md](SECURITY.md)。

---

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MUBU_PHONE` / `MUBU_PASSWORD` | — | 凭据，优先级高于本地存储 |
| `MUBU_READ_ONLY` | 关 | 设为 `1` 禁用所有创建类工具 |
| `MUBU_HOME` | `~/.mubu` | 凭据和 token 缓存的存放目录 |
| `MUBU_TIMEOUT` | `20` | 请求超时（秒） |
| `MUBU_MIN_INTERVAL_MS` | `200` | 两次请求之间的最小间隔 |
| `MUBU_MAX_RETRIES` | `2` | 网络错误和 5xx 的重试次数 |

---

## 原理说明

本项目直接对接幕布网页接口，其中几处是逆向出来的，记录在
[docs/mubu-api-notes.md](docs/mubu-api-notes.md)，最关键的几点：

* `POST /list/create_doc` 会**忽略**传入的 `content`，所以那样写不进正文（GitHub 上几个第三方项目都踩了这个坑）。
* 写正文需要用 `POST /list/import_doc`，报文是 `{name, folderId, itemCount, define}`，其中 `define` 是 `{"nodes": [...]}` 的 JSON 字符串，Markdown 在客户端本地解析成节点树。
* 所有请求都要带 `Jwt-Token`、`data-unique-id`、`x-session-id`、`x-request-id`、`x-reg-entrance`，否则部分接口直接回 `illegal request`。
* 覆盖已有文档走 WebSocket 协同编辑协议，本项目有意不实现。

---

## 已知限制

* 非官方接口，幕布改版就可能失效。
* 不能编辑/更新已有文档，不能删除、重命名、移动。
* Markdown 保真范围：标题、缩进列表、勾选、备注。折叠状态、有序列表编号、图片和附件不在范围内。
* `mubu_search` 是本地遍历（深度 ≤ 3、目录 ≤ 100、文档 ≤ 300），不是全文索引。
* 加密文档读不到，也不会出现在目录列表里。

---

## 开发

```bash
python -m unittest discover -s tests   # 64 个测试，不需要联网
ruff check src tests
mubu-web-mcp selftest
```

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 许可证

Apache-2.0，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
