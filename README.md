# ssh-mcp

把 SSH 凭据收进一个中间服务：客户端会话只拿 `alias` 就能执行命令，**永远看不到用户名、密码、私钥**。
配了一个原生桌面端（tkinter，无浏览器、无 Electron），增删主机就是在窗口里填表。

> **作者**：[初颜Sir](https://github.com/ChuYanSir) · chuyansir@163.com
> **仓库**：<https://github.com/ChuYanSir/ssh-mcp> · **版本**：v1.0.0

桌面端「更多 → 关于 ssh-mcp」和状态栏右下角都带有作者与仓库地址：

![关于](docs/ui-about.png)

```
        ┌──────────────┐    MCP (stdio / HTTP)     ┌──────────────────┐   SSH    ┌──────────────┐
        │  会话端 A     │ ────────────────────────▶ │                  │ ───────▶ │ prod-web-1   │
        │  会话端 B     │   只传 alias + command    │     ssh-mcp      │ ───────▶ │ prod-db-1    │
        │  会话端 C     │ ◀──────────────────────── │  (凭据 + 连接池)  │ ───────▶ │ bastion → 内网 │
        └──────────────┘   exit_code/stdout/stderr  └──────────────────┘          └──────────────┘
                                                          ▲
                                              ┌───────────┴────────────┐
                                              │ 桌面端：配置 / 测试 / 执行 │
                                              │ servers.yaml + 凭据文件   │
                                              └────────────────────────┘
```

---

## 1. 快速开始（桌面端）

**双击** `scripts\start-desktop.cmd`，或：

```powershell
cd E:\Temp\AiWork\ssh-mcp
.\.venv\Scripts\pythonw.exe -m ssh_mcp.desktop
# 指定配置：... -m ssh_mcp.desktop --config D:\ssh\servers.yaml
```

首次启动会自动创建 `servers.yaml`（在 cwd 或 `~/.ssh-mcp/` 下），窗口左侧是空的，点「新增主机」填表即可。
不需要装任何 GUI 依赖，Windows 自带 Python 的 tkinter 就够（本机已修好 venv 的 tkinter 支持）。

![桌面端](docs/ui-desktop.png)

---

## 2. 桌面端能做什么

| 区域 | 能力 |
|---|---|
| 左侧列表 | 别名 / 目标 / 用户 / 认证方式 / 标签 / **在线状态**（已连上的标绿） |
| 工具栏 | 新增主机、编辑、删除、**测试连接**、刷新、打开配置、打开凭据目录、**MCP 配置**（复制客户端 JSON） |
| 主机详情 | 地址、认证方式、密码来源标记（`file:…` / `env:…`，**不回显密码**）、sudo、超时、host key 策略、跳板机 |
| 命令终端 | 选一台主机直接跑命令，stdout / stderr / `[exit N]` 时间一次看全，支持 sudo 勾选 |
| 右上角 | MCP 端口 + Token + **启动/停止 MCP 服务**（同一进程内起，客户端会话直接连） |
| 状态栏 | 配置路径、主机数、在线数、审计开关、策略摘要 |

![新增主机对话框](docs/ui-desktop-add.png)

编辑对话框分三个页签：**基本信息 / 认证与 sudo / 高级**（core 字段、私钥或密码、凭据存储位置、
sudo 用户与密码、shell 类型、超时、host key 策略、跳板机）。

### 写入配置的四个保险

1. 保存前先在临时文件跑一遍 `load_config()` 校验，**通过才落盘**；
2. 覆盖前自动备份 `servers.yaml.bak`；
3. 密码默认不落明文：写到 `~/.ssh-mcp/creds/<alias>.pass`，yaml 里只留 `password_file`；
4. 保存后自动热重载配置 + 重建连接池，不用重启。

密码字段可选三种存储位置：**凭据文件（默认）/ 环境变量 / 直接写 yaml**；编辑时留空 = 保持原凭据。

### 桌面端和 MCP 服务的关系

桌面端是**遥控器**：点「启动 MCP 服务」会以**独立进程**拉起

```
python -m ssh_mcp --transport http --host 127.0.0.1 --port <端口> --config <servers.yaml> [--token <token>]
```

端点 `127.0.0.1:<端口>/mcp`。由此得到两个结果：

- **关掉桌面端窗口不影响服务** —— 客户端可以一直连着用；pid 记在 `~/.ssh-mcp/service.json`，
  下次打开桌面端会自动发现「● 运行中 :8765  pid 1234」并把按钮切成「停止 MCP 服务」。
- 关闭窗口时如果服务在跑，会问一句「要顺便把服务也停掉吗？」，选否就留着；
  服务日志在 `~/.ssh-mcp/logs/service.log`（更多 → 打开服务日志）。

![启动 MCP 服务](docs/ui-desktop-service.png)

启动成功时：按钮变成「停止 MCP 服务」、右上角亮绿灯 `● 运行中 :端口 pid N`、
终端面板打印端点地址和会话端该带的 `Authorization: Bearer …`。
端口/token 会记在 `~/.ssh-mcp/desktop-ui.json`，下次开窗口自动回填。

不想开桌面窗口时，也可以用命令行方式跑服务（见下节）。

### 一键复制客户端配置

工具栏「MCP 配置」给出两段可直接粘贴的 JSON，各带一个「复制」按钮：

![MCP 配置](docs/ui-mcp-config.png)

- **HTTP（服务模式，推荐）**：`type: streamable-http` + `url` + `Authorization: Bearer <token>`。
  客户端连上后用的就是桌面端这个进程里的凭据与连接池；服务没启动时这里标黄提示。
- **stdio（不用常驻服务）**：`command` 指向 venv 的 `python.exe`（不是 `pythonw.exe`），
  `args` 里带 `--config` 的绝对路径，由客户端自己拉起子进程。

点「复制」后直接粘进客户端的 `mcpServers`（DeepSeek Harness / Claude Desktop / Cursor 都认这个结构）。
如果客户端把类型写成 `http` / `streamable_http`，改 `type` 的值即可，`url` 与 `headers` 不用动。

---

## 3. 当成常驻服务用

```powershell
# HTTP（常驻，多会话共用一套凭据与连接池）
.\scripts\start-http.ps1                     # 自动生成 token，打印地址
.\scripts\start-http.ps1 -NoAuth             # 本地回环自用，免 token
.\scripts\start-http.ps1 -Port 9000 -Token xxx

# stdio（客户端自己拉起子进程，适合单会话）
.\.venv\Scripts\python.exe -m ssh_mcp --transport stdio --config servers.yaml

# 只校验配置解析（不连接，凭据已脱敏）
.\.venv\Scripts\python.exe -m ssh_mcp --config servers.yaml --print-config
```

| | 桌面端 | HTTP 服务（桌面端拉起或 `start-http.ps1`） | stdio |
|---|---|---|---|
| 图形界面 | ✅ 唯一的图形入口 | ❌ 纯 MCP 端点 | ❌ |
| 进程 | GUI 进程 | **独立进程，关 GUI 不影响** | 客户端拉起 |
| 连接池 | 自己的（测试/终端用） | 自己的，多会话共享 | 各进程独立 |
| 适合 | 本机运维/管理 | 团队共用一台跳板 | 单机单 Agent |

> HTTP 模式只暴露 `/mcp` 一个端点，根路径返回 404——**没有网页后台**，
> 所有配置、测试、执行都在桌面端做。

配置文件查找顺序：`--config` → `SSH_MCP_CONFIG` → `./servers.yaml`
→ `./.ssh-mcp/servers.yaml` → `~/.ssh-mcp/servers.yaml`。

---

## 4. 接入客户端

### 4.1 DeepSeek Harness（本机）

HTTP 方式（桌面端或 start-http.ps1 起着服务时）：

```json
{
  "mcpServers": {
    "ssh": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer 你的token" }
    }
  }
}
```

stdio 方式（每个会话一个子进程）：

```json
{
  "mcpServers": {
    "ssh": {
      "command": "E:\\Temp\\AiWork\\ssh-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "ssh_mcp", "--transport", "stdio", "--config", "E:\\Temp\\AiWork\\ssh-mcp\\servers.yaml"]
    }
  }
}
```

### 4.2 Claude Desktop / Cursor / 其他 stdio 客户端

```json
{
  "mcpServers": {
    "ssh": {
      "command": "E:\\Temp\\AiWork\\ssh-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "ssh_mcp", "--transport", "stdio"],
      "env": { "SSH_MCP_CONFIG": "E:\\Temp\\AiWork\\ssh-mcp\\servers.yaml" }
    }
  }
}
```

> 会话端配置里**没有**任何 SSH 账号密码 —— 这是本项目的核心目标。

---

## 5. 配置说明（servers.yaml）

> 用桌面端时不需要读这节；想批量导入或纳管到版本库再看。

### 5.1 全局默认 `defaults`

| 字段 | 默认 | 说明 |
|---|---|---|
| `port` | 22 | SSH 端口 |
| `connect_timeout` | 15 | 建连 + 握手超时（秒） |
| `command_timeout` | 120 | 单条命令默认超时（秒） |
| `max_output_bytes` | 262144 | stdout / stderr 各自上限 |
| `keepalive` | 30 | keepalive 间隔，0 关闭 |
| `host_key_policy` | `auto_add` | `strict` 只认 known_hosts / `auto_add` 首次自动记录 / `ignore` 不校验 |
| `known_hosts` | `~/.ssh/known_hosts` | 私有 known_hosts 存在时用它并写回 |
| `encoding` | `utf-8` | 远端输出解码 |
| `shell_kind` | `posix` | `posix` / `powershell` / `cmd` |
| `posix_shell` | `bash` | posix 下用哪个 shell 包命令，Alpine 改 `sh` |
| `max_concurrency` | 4 | 同一主机并发命令上限 |
| `default_cwd` | — | 每条命令执行前 `cd` 到这里 |

### 5.2 主机 `hosts.<alias>`

必填只有 `host`。常用：`port`、`user`、`description`、`tags`、`auth`、`sudo`、
`shell_kind`、`default_cwd`、`command_timeout`、`proxy_jump`。

```yaml
auth:                                   # 私钥（推荐）
  method: key
  key_file: ~/.ssh/id_ed25519
  key_passphrase_env: PROD_KEY_PASSPHRASE

auth:                                   # 密码：环境变量 / 凭据文件 / 字面量
  method: password
  password_env: PROD_DB_SSH_PASS
  # password_file: C:\Users\you\.ssh-mcp\creds\prod-db.pass

auth:                                   # 用 ssh-agent 里已加载的 key
  method: agent

sudo:
  password_env: PROD_SUDO_PASS
  user: root

proxy_jump: bastion                     # 经另一台已配置的 alias 进内网
```

凭据解析优先级：字面量 > `*_env` > `*_file`。生产建议只用后两种。

### 5.3 策略 `policy`

| 字段 | 作用 |
|---|---|
| `deny_commands` | 正则黑名单（默认拦 `rm -rf /`、fork bomb、`mkfs`、裸 `dd of=/dev/sdX`） |
| `allow_commands` | 正则白名单，非空时命令必须命中其一 |
| `max_command_timeout` / `max_output_bytes` | 单次硬上限 |
| `allow_sudo` / `allow_upload` / `allow_download` / `allow_write_file` | 能力开关 |
| `allowed_hosts` | 非空时只放开列出的 alias（多租户隔离） |
| `dry_run` | 只校验不执行 |

### 5.4 审计 `audit`

每次 exec / 文件操作 / 桌面端的配置改动都落一行 JSONL，`ssh_audit_tail` 工具可直接查。

---

## 6. 工具清单（12 个）

| 工具 | 作用 |
|---|---|
| `ssh_list_hosts` | 列出别名、主机、说明、标签、认证方式（不含凭据） |
| `ssh_exec` | **核心**：`alias` + `command`，支持 `cwd` / `timeout` / `sudo` / 输出上限 |
| `ssh_exec_batch` | 多机并发跑同一条命令 |
| `ssh_check` | 握手探测 + 返回 `uname` / `id` / `date` |
| `ssh_upload` / `ssh_download` | SFTP 双向传文件，返回 sha256 校验 |
| `ssh_write_file` / `ssh_read_file` | 直接读写远端文本文件 |
| `ssh_status` | 连接池状态 + 生效策略 + 配置来源 |
| `ssh_close` | 主动断开连接（下次调用自动重连） |
| `ssh_reload_config` | 热重载 `servers.yaml` |
| `ssh_audit_tail` | 最近审计记录 |

返回统一 JSON：`{alias, host, command, exit_code, ok, timed_out, truncated, duration_ms, stdout, stderr}`。

---

## 7. 安全模型

1. **工具参数里没有 host / user / password / key**：想连新机器只能改服务端配置或桌面端，
   会话端拿不到也传不进凭据。
2. **凭据解析只在服务端**：列表与状态只回 `env:XXX`、`file:XXX`、`literal` 这类来源标记。
3. **写配置四道保险**：先校验 → 再备份 → 才覆盖 → 密码走独立凭据文件。
4. **HTTP 端点**用 Bearer token 保护；非回环监听且没 token 会告警。
5. **Host key 校验**可切 `strict`，私有 `known_hosts` 自动写回，不污染系统文件。
6. **命令策略**双层（黑名单保底 + 白名单收口）+ **审计**留痕。
7. 进程本身是唯一信任点：`servers.yaml`、`~/.ssh-mcp/` 权限按系统账号收紧。

---

## 8. 常见问题

- **桌面端打不开**：用 `python -m ssh_mcp.desktop` 前台跑一遍看报错；tkinter 缺失时按
  `docs/` 里记的方式给 Python 补 tcl/tk（本机已补）。
- **关了桌面端，MCP 服务会停吗**：不会。服务是独立进程（pid 记在 `~/.ssh-mcp/service.json`），
  关窗口时只会问你要不要顺便停掉；之后想停可以在窗口里点「停止 MCP 服务」，
  或直接结束那个 pid（`taskkill /PID <pid> /F`）。
- **点「启动 MCP 服务」没反应**：先看终端面板和日志 `~/.ssh-mcp/logs/desktop.log`、`~/.ssh-mcp/logs/service.log`。
  早期版本在 `pythonw` 下起不来，是因为 `sys.stdout/sys.stderr` 为 `None`，
  uvicorn 的默认 formatter 会调 `sys.stdout.isatty()` 抛 `AttributeError`，
  而 tkinter 回调异常默认被丢进黑洞；现已用 `ensure_stdio()`（重定向到日志）+ 
  `Config(use_colors=False)` 双保险修掉。端口被占会在终端面板直接提示。
- **`bash: not found`**：目标机是 Alpine/BusyBox，`posix_shell` 填 `sh`。
- **远端是 Windows**：`shell_kind` 选 `powershell` 或 `cmd`，命令按对应语法写。
- **`sudo: a password is required`**：在桌面端「认证与 sudo」页填密码，或目标机配 `NOPASSWD`。
- **输出被截断**：返回 `truncated: true`，调大上限或把命令重定向到文件后用 `ssh_read_file` 分段读。
- **长时间任务**：exec 超时不会杀远端进程，建议 `nohup cmd > log 2>&1 &` 后轮询日志。
- **手改 yaml 后不生效**：桌面端点「刷新」；HTTP 服务调 `ssh_reload_config`；stdio 重启客户端。
- **桌面端保存后 yaml 注释没了**：`safe_dump` 不保留注释，原文件在 `servers.yaml.bak`；
  想保留注释就手写。

**已知边界**：不提供交互式 shell、端口转发、SCP；一条 exec 只跑一条命令，多步用 `&&` 串。

---

## 9. 自测

```powershell
cd E:\Temp\AiWork\ssh-mcp
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest tests -q        # 20 项：exec/退出码/截断/策略/SFTP/MCP stdio/配置读写
.\.venv\Scripts\python.exe tests\http_probe.py       # HTTP + Bearer 401/200，且根路径必须 404
.\.venv\Scripts\python.exe tests\desktop_demo.py     # 桌面端 + 假 SSH 后端（127.0.0.1 随机端口）
.\.venv\Scripts\python.exe tests\probe_hosts.py      # 逐台探测 servers.yaml 里配置的 SSH 是否可达
```

测试用 `tests/fake_ssh_server.py` 起本地回环 SSH 服务（密码认证 + exec + SFTP），不碰任何真实主机。

---

## 10. 作者与许可

| | |
|---|---|
| 作者 | 初颜Sir |
| 邮箱 | chuyansir@163.com |
| 仓库 | <https://github.com/ChuYanSir/ssh-mcp> |
| 问题反馈 | <https://github.com/ChuYanSir/ssh-mcp/issues> |

界面上的入口：

- 桌面端：右上「更多 ▾ → 关于 ssh-mcp」（含复制地址、打开 GitHub 按钮），状态栏右下角常驻
  `by 初颜Sir · github.com/ChuYanSir/ssh-mcp`，点一下即可打开关于窗口。
- 作者、邮箱、仓库地址与版本号统一放在 `src/ssh_mcp/__init__.py`（`__author__` /
  `__author_email__` / `__github__` / `__version__`），改一处即可。
