# Custos：托管 Skill 平台（本地 Mock）

Custos 是单进程、零第三方依赖的 Python 原型，用于验证 Skill 创建、远程配置、Harness 下载、动态环境变量注入和临时脚本执行。

## 管理页面

- **Skill 管理**（`/`）：创建并发布两个初始化示例，也可自然语言生成其他 Skill 草稿，人工修改后发布；下载 Harness 包目前支持 OpenCode。
- **运行时管理**（`/runtime-page`）：设置最长运行时间、CPU/内存上限、外网白/黑名单。
- **环境变量管理**（`/secrets-page`）：查看和配置变量名、值及团队范围。Skill 仅声明所需变量名；运行时检查团队权限后，只向本次子进程注入声明过的变量。

初始化示例：

1. **周报助手**：服务端运行时 Prompt 要求输出标题“Custos 团队周报”。标题不在客户端下载的 `SKILL.md` 中，展示远程 Prompt 变更无需重新下载。
2. **Custos 团队通讯录**：服务启动时在临时目录创建假联系人 CSV，`CUSTOS_CONTACTS_DIR` 保存该目录的绝对路径。Skill 声明该变量，并通过 `run_code` 列出目录文件和读取联系人。

下载 ZIP 包含 OpenCode 标准位置 `.opencode/skills/<skill-id>/SKILL.md`、`opencode.jsonc`，及 ZIP 根目录可见的 `SKILL.md` 副本。OpenCode 运行时通过 MCP 获取服务端最新 Skill 配置；Harness 只使用通用 `run_code` 接口，没有固定业务工具注册。

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
| GET | `/`、`/runtime-page`、`/secrets-page` | 三个管理页面 |
| GET | `/skills` | 已发布 Skill 列表 |
| POST | `/skill/generate` | 自然语言生成 Skill 草稿 |
| POST | `/skill/publish` | 发布 Skill 新版本 |
| GET | `/skill/{id}/config` | 获取最新服务端配置及所需环境变量名 |
| GET | `/skill/{id}/download?harness=opencode` | 下载 OpenCode Skill 包 |
| GET/POST | `/runtime`、`/runtime/configure` | 查询和设置运行时资源策略 |
| GET | `/secrets` | 列出环境变量名和授权团队，不返回值 |
| POST | `/secrets/configure`、`/secrets/remove` | 管理服务端环境变量 |
| POST | `/sandbox/run` | 将临时代码交给 Python 子进程运行 |
| POST | `/mcp` | MCP JSON-RPC：`get_skill_config`、通用 `run_code` |
| GET | `/model/config` | 查看模型模式，不返回 API Key |
| GET | `/audit/query` | 查询内存审计记录 |

页面使用固定 Mock 管理员身份，真实认证未实现。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

> **安全限制：** 子进程是本地模拟，不是真正沙箱。最长运行时间和 CPU/内存设置会尽可能映射到系统资源限制；网络规则当前只保存、不强制执行。子进程仍可能访问宿主机文件和网络，也能读取注入的路径/环境变量。仅使用假联系人数据和可信代码；生产环境必须换成有文件、网络和资源隔离的容器/VM/专用沙箱。
