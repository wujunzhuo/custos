# Custos：托管 Skill 平台（本地 Mock）

Custos 是一个单进程、零第三方依赖的 Python 原型，用于演示托管 Skill 配置、MCP 工具调用，以及 WorkBuddy / OpenCode 客户端接入。

## 设计意图

### 原型范围

- 每个 `skill_id` 只保存一份当前配置；再次发布会覆盖现有配置。
- 下载时为每个 Skill 单独选择 WorkBuddy 或 OpenCode。
- Agent 通过 MCP 获取服务端当前 Skill 配置，并按需调用 `run_code`。
- Skill、环境变量和运行时事件只存在于当前进程；服务重启后重置。

### 客户端凭证

- ZIP 包里不放个人 Key。
- **WorkBuddy**：MCP 服务在客户端统一配置。Skill ZIP 只附配置说明和示例；每位用户在自己的 WorkBuddy 配置中填入个人 Key，MCP 只需配置一次，可供多个 Custos Skill 复用。
- **OpenCode**：下载包中的配置从 `CUSTOS_MCP_KEY` 环境变量读取 Key；用户可将 Key 保存在本机凭证文件中，再于启动 OpenCode 前加载。
- Mock MCP Key 的格式是 `csk_` 加 32 位小写十六进制字符。服务端只校验格式：任何格式正确的 Key 都能调用，不验证 Key 是否由管理员签发，也不按用户身份分配权限。
- 日志中的 `user_id` 取 Key 末 6 位，仅作为可观测性展示标签；末 6 位相同的 Key 会显示成相同标签。

管理员可为每位用户生成一个符合格式的示例 Key：

```sh
python3 -c 'import secrets; print("csk_" + secrets.token_hex(16))'
```

### 管理界面与示例数据

- **Skill 管理**（`/`）：创建、编辑并发布当前 Skill；生成模拟测试提问；下载客户端包。
- **环境变量管理**（`/secrets-page`）：管理服务端运行 Skill 所需的环境变量。仅向本次 `run_code` 执行注入 Skill 声明的变量。
- **运行时日志**（`/runtime-logs-page`）：独立的可观测性模块，记录 MCP 工具调用和 `run_code` 执行摘要；不记录代码、Key 或环境变量值。
- **周报助手**：服务端 Prompt 要求输出标题“Custos 周报”，无需将完整运行时 Prompt 放进客户端 Skill 文件。
- **Custos 通讯录**：服务启动时创建包含约 20 位不同岗位联系人的临时 CSV；`CUSTOS_CONTACTS_CSV` 保存文件路径，进程退出时清理。

### 原型限制

此项目没有真实用户认证、Key 签发校验、按用户授权或生产级密钥管理。管理页面使用固定 Mock 管理员身份；环境变量管理 API 会返回明文值。`run_code` 在临时 Python 子进程执行，但没有沙箱隔离。**仅适用于本地原型，不要放入生产凭证或暴露给不可信网络。**

## 使用方法

### 1. 启动 Custos 服务

需要 Python 3.10+，无需安装依赖。在项目目录运行：

```sh
python3 app.py
```

默认监听 `http://127.0.0.1:8000`。保持该进程运行，管理页面位于 <http://127.0.0.1:8000/>。

Agent 客户端必须能访问 MCP 地址。如果客户端不在运行 Custos 的同一台机器上，应配置客户端可访问的地址，并确保网络可达。默认 HTTP 地址只适合本地 Mock 测试。

可选：配置 OpenAI-compatible 模型后再启动，Skill 草稿和测试提问会使用远程模型；未配置时使用本地 Mock。

```sh
export MODEL_BASE_URL="https://your-model-endpoint/v1"
export MODEL_API_KEY="your-model-api-key"
export MODEL_NAME="your-model-name"
python3 app.py
```

也支持 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。API Key 不会通过模型配置接口返回。

### 2. 管理员准备 Skill 和个人 Key

1. 打开 Custos 管理页，创建或编辑 Skill 并点击保存/发布。
2. 为每位用户单独生成一个符合格式的 Key，通过安全渠道发给对应用户。不要把 Key 写进 ZIP 或共享配置。
3. 在目标 Skill 行点击“下载”，选择 `WorkBuddy` 或 `OpenCode`。不同 Skill 可选择不同客户端。

### 3. WorkBuddy 用户连接

WorkBuddy 下载包是普通 Skill ZIP，不是 Connector 包；不需要上架、审核或发布 Connector。ZIP 不含 `mcp.json` 文件，只在 `CUSTOS-SETUP.txt` 提供配置示例。Skill 导入和 MCP 服务配置是两个独立步骤；每位用户的 Custos MCP 在 WorkBuddy 客户端统一配置一次。

每位用户在自己的 WorkBuddy 客户端操作：

1. 首次使用时，打开 WorkBuddy 的用户级/全局 MCP 自定义配置，将 ZIP 中 `CUSTOS-SETUP.txt` 的 `custos` 条目合并进现有 `mcpServers`。每位用户只需配置一次；其他 Custos Skill 会复用此 MCP 服务。不要覆盖配置中的其他 MCP 服务。配置条目类似：

   ```json
   {
     "custos": {
       "type": "streamableHttp",
       "url": "http://127.0.0.1:8000/mcp",
       "timeout": 30000,
       "headers": {
         "Authorization": "Bearer REPLACE_WITH_YOUR_PERSONAL_KEY"
       }
     }
   }
   ```

   使用 ZIP 中生成的 `url`；如果 WorkBuddy 无法访问该地址，改成客户端可访问的 Custos 地址。将 `Authorization` 的占位值 `REPLACE_WITH_YOUR_PERSONAL_KEY` 替换成管理员发放给自己的 Key，然后保存配置。
2. 将下载的 Skill ZIP 导入 WorkBuddy 的“技能”页面。之后下载其他 Custos Skill 时，只需导入 Skill，不必重复配置 MCP。
3. 在 WorkBuddy 对话中使用 Skill；MCP 请求会自动带上该 Header。

个人 Key 会以明文写入用户自己的 MCP 配置；不要将该配置共享给其他用户、提交到代码仓库或放入 Skill ZIP。WorkBuddy 的 MCP 配置格式参见[连接器文档中的 `mcp.json` 说明](https://open.workbuddy.cn/docs/connector)。

### 4. OpenCode 用户连接

1. 下载时选择 `OpenCode`，将 ZIP 解压到 OpenCode 项目目录。
2. 将 Key 存在本机文件 `~/.config/custos/credentials.env`，不要放入项目或 ZIP：

   ```sh
   umask 077
   mkdir -p ~/.config/custos
   nano ~/.config/custos/credentials.env  # 写入 CUSTOS_MCP_KEY=csk_... 后保存
   chmod 600 ~/.config/custos/credentials.env
   ```

3. 在同一个终端加载文件并启动 OpenCode：

   ```sh
   set -a
   . ~/.config/custos/credentials.env
   set +a
   opencode
   ```

   PowerShell 也可在当前会话中设置变量后启动：

   ```powershell
   $env:CUSTOS_MCP_KEY = '管理员发放的个人 Key'
   opencode
   ```

`opencode.jsonc` 只引用 `{env:CUSTOS_MCP_KEY}`，不包含 Key 明文。

### 5. 查看运行时日志

在 Agent 对话中调用 Skill 后，打开 Custos 的“运行时日志”页面。页面会显示 MCP 工具调用、代码执行摘要、耗时和 `user_id`（Key 末 6 位）。MCP Key 和代码正文不会写入运行时日志。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/`、`/secrets-page`、`/runtime-logs-page` | 管理页面 |
| GET | `/skills` | 已发布 Skill 列表 |
| POST | `/skill/generate` | 生成 Skill 草稿 |
| POST | `/skill/test-questions` | 生成模拟测试提问 |
| POST | `/skill/publish` | 新建或覆盖当前 Skill 配置 |
| GET | `/skill/{id}/config` | 获取最新 Skill 配置 |
| GET | `/skill/{id}/download?harness=workbuddy` 或 `harness=opencode` | 下载客户端包 |
| GET | `/secrets` | 列出环境变量名和值（POC） |
| POST | `/secrets/configure`、`/secrets/remove` | 管理服务端环境变量 |
| POST | `/mcp` | MCP JSON-RPC：`get_skill_config`、`run_code`；需格式正确的 Bearer Key |
| GET | `/runtime-logs` | 查询 MCP 调用和工具执行运行时事件 |
| GET | `/model/config` | 查看模型模式，不返回 API Key |

服务重启后 Skill、环境变量和运行时事件重置；示例联系人临时 CSV 会随服务退出清理。

## 测试

```sh
python3 -m unittest discover -s tests -v
```
