# Custos：托管 Skill 平台（本地 Mock）

Custos 是单进程、零第三方依赖的 Python 原型，用于验证 Skill 创建、远程配置、Harness 下载、动态环境变量注入和临时脚本执行。

## 管理页面

- **Skill 管理**（`/`）：创建并发布两个初始化示例，也可自然语言生成其他 Skill 草稿，人工修改后发布；可根据 Skill 定义和用户引导生成模拟测试提问，并下载 WorkBuddy Skill 包。
- **环境变量管理**（`/secrets-page`）：查看和配置变量名、明文值及团队范围。`GET /secrets` 同样返回明文值（POC 专用）；审计记录不包含变量值。Skill 仅声明所需变量名；运行时检查团队权限后，只向本次子进程注入声明过的变量。
- **运行时日志**（`/runtime-logs-page`）：查询 WorkBuddy 通过 MCP 调用 Skill 工具的历史记录，只展示调用摘要，不保存代码或密钥值。

初始化示例：

1. **周报助手**：服务端运行时 Prompt 要求输出标题“Custos 团队周报”。标题不在客户端下载的 `SKILL.md` 中，展示远程 Prompt 变更无需重新下载。
2. **Custos 团队通讯录**：服务启动时在临时目录创建假联系人 CSV，`CUSTOS_CONTACTS_DIR` 保存该目录的绝对路径。Skill 声明该变量，并通过 `run_code` 列出目录文件和读取联系人。

下载 ZIP 包含 WorkBuddy Skill 目录 `skills/<skill-id>/SKILL.md`、用于连接 Custos MCP 服务的 `mcp.json`，及 ZIP 根目录可见的 `SKILL.md` 副本。WorkBuddy 运行时通过 MCP 获取服务端最新 Skill 配置；Harness 只使用通用 `run_code` 接口，没有固定业务工具注册。

## WorkBuddy 安装与连接

1. **启动 Custos**：运行 `python3 app.py`，保持服务运行。默认地址为 `http://127.0.0.1:8000`。
2. **下载 Skill 包**：打开 Custos 的 Skill 管理页，在目标 Skill 行点击“下载”。保存生成的 ZIP 文件。
3. **导入 Skill**：在 WorkBuddy 打开“技能”页面，点击“添加技能”→“上传技能”，选择下载的 ZIP 并完成导入。导入后即可在对话中使用该 Skill。
4. **配置 Custos MCP 连接器**：在 WorkBuddy 打开“连接器”→“自定义连接器”，新增 MCP 连接器。将以下配置粘贴到 MCP 配置编辑器（或保存为 `mcp.json` 后导入），并按需修改服务地址和请求头：

   ```json
   {
     "mcpServers": {
       "custos": {
         "type": "streamableHttp",
         "url": "http://127.0.0.1:8000/mcp",
         "timeout": 30000,
         "headers": {
           "X-User-ID": "demo-user",
           "X-Team-ID": "custos-team"
         }
       }
     }
   }
   ```

   保存并启用连接器。ZIP 内的 `mcp.json` 使用相同格式，并根据下载时的访问地址和 Skill 灰度团队生成；WorkBuddy 如果使用表单配置，就将 JSON 中的 `url`、`type` 和 `headers` 分别填入对应字段。
5. **验证调用**：在 WorkBuddy 对话里提出与 Skill 相关的问题；回到 Custos 的“运行时日志”页面查看 `get_skill_config`、`run_code` 等工具调用记录。

WorkBuddy 必须能访问 Custos 的服务地址。如果 WorkBuddy 不在运行 Custos 的同一台机器上，请将 `127.0.0.1` 改为 WorkBuddy 可访问的主机地址，并确保网络可达。MCP 请求头中的团队必须符合 Skill 的灰度范围；示例 Skill 使用 `custos-team`。以上身份仅用于本地 Mock，不是真实认证。

参考：[WorkBuddy 技能文档](https://workbuddy.ai/docs/zh/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills) · [WorkBuddy 连接器文档](https://workbuddy.ai/docs/zh/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector)。

## 启动

需要 Python 3.10+，无需安装依赖：

```sh
python3 app.py
```

访问 <http://127.0.0.1:8000/>。服务重启后 Skill、变量和审计记录重置，示例联系人临时目录会随服务退出清理。

若要使用 OpenAI-compatible 模型生成 Skill，在启动前设置：

```sh
export MODEL_BASE_URL="https://your-model-endpoint/v1"
export MODEL_API_KEY="your-api-key"
export MODEL_NAME="your-model-name"
python3 app.py
```

也支持 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。未配置 Base URL 或模型名时，生成走本地 Mock。API Key 不会通过页面或配置查询接口返回。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/`、`/secrets-page`、`/runtime-logs-page` | 管理页面 |
| GET | `/skills` | 已发布 Skill 列表 |
| POST | `/skill/generate` | 自然语言生成 Skill 草稿 |
| POST | `/skill/test-questions` | 根据已发布 Skill 和用户引导生成模拟提问 |
| POST | `/skill/publish` | 发布 Skill 新版本 |
| GET | `/skill/{id}/config` | 获取最新服务端配置及所需环境变量名 |
| GET | `/skill/{id}/download?harness=workbuddy` | 下载 WorkBuddy Skill 包 |
| GET/POST | `/runtime`、`/runtime/configure` | 查询和设置运行时资源策略 |
| GET | `/secrets` | 列出环境变量名、明文值和授权团队（POC） |
| POST | `/secrets/configure`、`/secrets/remove` | 管理服务端环境变量 |
| POST | `/sandbox/run` | 将临时代码交给 Python 子进程运行 |
| POST | `/mcp` | MCP JSON-RPC：`get_skill_config`、通用 `run_code` |
| GET | `/model/config` | 查看模型模式，不返回 API Key |
| GET | `/audit/query` | 查询内存审计记录 |
| GET | `/runtime-logs` | 查询历史 MCP 工具调用 |

页面使用固定 Mock 管理员身份，真实认证未实现。环境变量值在 API 和管理页面明文显示，仅适用于本地 POC；不要配置生产凭据或部署到不可信网络。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

> **安全限制：** 子进程是本地模拟，不是真正沙箱。最长运行时间和 CPU/内存设置会尽可能映射到系统资源限制；网络规则当前只保存、不强制执行。子进程仍可能访问宿主机文件和网络，也能读取注入的路径/环境变量。仅使用假联系人数据和可信代码；生产环境必须换成有文件、网络和资源隔离的容器/VM/专用沙箱。
