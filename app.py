#!/usr/bin/env python3
"""A dependency-free managed Skill and tool-execution prototype."""

from __future__ import annotations

import json
import hashlib
import atexit
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from io import BytesIO
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def now() -> float:
    return time.time()


def trace_id(prefix: str = "audit") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


MODEL_BASE_URL = os.environ.get("MODEL_BASE_URL", os.environ.get("LLM_BASE_URL", "")).strip()
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", os.environ.get("LLM_API_KEY", ""))
MODEL_NAME = os.environ.get("MODEL_NAME", os.environ.get("LLM_MODEL", "")).strip()


@dataclass
class UserContext:
    user_id: str
    token: str = "mock-token"
    permissions: list[str] = field(default_factory=lambda: ["user"])

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None, default: UserContext | None = None) -> UserContext:
        if value is None:
            if default is None:
                raise ValueError("缺少 user_ctx")
            return default
        if not isinstance(value, dict) or not value.get("user_id"):
            raise ValueError("user_ctx 必须包含 user_id")
        return cls(
            user_id=str(value["user_id"]),
            token=str(value.get("token", "mock-token")),
            permissions=list(value.get("permissions", ["user"])),
        )


@dataclass
class SkillMeta:
    skill_id: str
    name: str
    description: str
    version: str
    system_prompt_snippet: str
    output_schema: dict[str, Any]
    outline: list[dict[str, Any]]
    required_env_vars: list[str] = field(default_factory=list)
    update_at: float = field(default_factory=now)


@dataclass
class SecretEnvVar:
    name: str
    value: str = field(repr=False)


class Store:
    """All state is deliberately process-local and is reset on restart."""

    def __init__(self) -> None:
        self.skills: dict[str, list[SkillMeta]] = {}
        self.rollback_targets: dict[str, str] = {}
        self.secrets: dict[str, SecretEnvVar] = {}
        self.audit: list[dict[str, Any]] = []

    def log(self, user_id: str, action_type: str, *, skill_id: str | None = None,
            execution_id: str | None = None,
            tool_name: str | None = None, input_snapshot: Any = None,
            result_preview: Any = None, tags: list[str] | None = None) -> str:
        ref = trace_id()
        self.audit.append({
            "trace_id": ref, "user_id": user_id, "skill_id": skill_id,
            "execution_id": execution_id, "action_type": action_type,
            "tool_name": tool_name,
            "input_snapshot": input_snapshot, "result_preview": result_preview,
            "timestamp": now(), "tags": tags or [],
        })
        return ref


class SkillService:
    def __init__(self, store: Store):
        self.store = store

    def publish(self, meta: SkillMeta, operator: UserContext) -> dict[str, Any]:
        if "admin" not in operator.permissions:
            return {"ok": False, "msg": "无权限发布skill"}
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", meta.skill_id) or len(meta.skill_id) > 64:
            return {"ok": False, "msg": "skill_id须为1-64位小写字母、数字和连字符"}
        if not meta.name.strip() or not meta.description.strip() or not meta.system_prompt_snippet.strip():
            return {"ok": False, "msg": "名称、描述和运行时提示词不能为空"}
        if not isinstance(meta.outline, list) or not isinstance(meta.output_schema, dict):
            return {"ok": False, "msg": "outline必须是数组，output_schema必须是对象"}
        if not isinstance(meta.required_env_vars, list) or any(
                not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)
                for name in meta.required_env_vars):
            return {"ok": False, "msg": "required_env_vars必须是合法环境变量名数组"}
        if len(meta.required_env_vars) > 32 or len(set(meta.required_env_vars)) != len(meta.required_env_vars):
            return {"ok": False, "msg": "required_env_vars最多32个且不能重复"}
        versions = self.store.skills.setdefault(meta.skill_id, [])
        if any(item.version == meta.version for item in versions):
            return {"ok": False, "msg": "该skill版本已存在"}
        meta.update_at = now()
        versions.append(meta)
        self.store.rollback_targets.pop(meta.skill_id, None)
        self.store.log(operator.user_id, "skill_publish", skill_id=meta.skill_id,
                       result_preview={"version": meta.version})
        return {"ok": True, "data": asdict(meta)}

    def effective(self, skill_id: str, user: UserContext, *, audit: bool = True) -> dict[str, Any]:
        versions = self.store.skills.get(skill_id, [])
        if not versions:
            return {"ok": False, "msg": "skill不存在"}
        target = self.store.rollback_targets.get(skill_id)
        candidates = [v for v in versions if not target or v.version == target]
        if not candidates:
            return {"ok": False, "msg": "当前没有可用的skill版本"}
        selected = max(candidates, key=lambda item: item.update_at)
        if audit:
            self.store.log(user.user_id, "skill_fetch", skill_id=skill_id,
                           input_snapshot={"skill_id": skill_id},
                           result_preview={"version": selected.version})
        return {"ok": True, "data": asdict(selected)}

    def rollback(self, skill_id: str, target_version: str, operator: UserContext) -> dict[str, Any]:
        if "admin" not in operator.permissions:
            return {"ok": False, "msg": "无权限"}
        versions = self.store.skills.get(skill_id, [])
        target = next((v for v in versions if v.version == target_version), None)
        if target is None:
            return {"ok": False, "msg": "目标版本不存在"}
        self.store.rollback_targets[skill_id] = target_version
        self.store.log(operator.user_id, "skill_rollback", skill_id=skill_id,
                       result_preview={"version": target_version})
        return {"ok": True, "data": {"skill_id": skill_id, "effective_version": target_version}}

    def latest_versions(self) -> list[SkillMeta]:
        return [max(versions, key=lambda item: item.update_at)
                for versions in self.store.skills.values() if versions]


class ToolExecutionService:
    """Run prototype tool code in a temporary child process; this is not a sandbox."""

    MAX_CODE_BYTES = 64 * 1024
    MAX_OUTPUT_BYTES = 256 * 1024
    TIMEOUT_SECONDS = 30

    def __init__(self, store: Store, skills: SkillService):
        self.store, self.skills = store, skills

    @staticmethod
    def _redact(text: str, secrets: dict[str, str]) -> str:
        for secret in sorted((value for value in secrets.values() if value), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def run(self, skill_id: str, code: str, user: UserContext) -> dict[str, Any]:
        if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > self.MAX_CODE_BYTES:
            return {"ok": False, "msg": "代码不能为空且不能超过64KB"}
        effective = self.skills.effective(skill_id, user, audit=False)
        if not effective["ok"]:
            return effective
        required_names = effective["data"].get("required_env_vars", [])
        runtime_secrets: dict[str, str] = {}
        for name in required_names:
            secret = self.store.secrets.get(name)
            if secret is None:
                return {"ok": False, "msg": f"Skill需要的环境变量 {name} 尚未由管理员配置"}
            runtime_secrets[name] = secret.value
        if sum(len(name) + len(value) for name, value in runtime_secrets.items()) > 64 * 1024:
            return {"ok": False, "msg": "本次申请的环境变量总大小超过64KB"}

        execution_id = trace_id("exec")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="custos-run-") as workdir:
            script_path = os.path.join(workdir, "main.py")
            with open(script_path, "w", encoding="utf-8") as script:
                script.write(code)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": workdir,
                   "TMPDIR": workdir, "TEMP": workdir, "TMP": workdir,
                   "PYTHONIOENCODING": "utf-8"}
            if os.name == "nt" and os.environ.get("SYSTEMROOT"):
                env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            env.update(runtime_secrets)
            stdout_file, stderr_file = tempfile.TemporaryFile(), tempfile.TemporaryFile()
            bootstrap = "import runpy, sys\nrunpy.run_path(sys.argv[1], run_name='__main__')\n"
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-c", bootstrap, script_path],
                    cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                    stdout=stdout_file, stderr=stderr_file,
                    start_new_session=(os.name != "nt"),
                )
            except OSError as exc:
                stdout_file.close()
                stderr_file.close()
                return {"ok": False, "msg": f"无法启动工具子进程：{exc}"}
            timed_out = False
            try:
                process.wait(timeout=self.TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(self.MAX_OUTPUT_BYTES + 1).decode("utf-8", errors="replace")
            stderr = stderr_file.read(self.MAX_OUTPUT_BYTES + 1).decode("utf-8", errors="replace")
            stdout_file.close()
            stderr_file.close()
            truncated = len(stdout.encode("utf-8")) > self.MAX_OUTPUT_BYTES or len(stderr.encode("utf-8")) > self.MAX_OUTPUT_BYTES
            stdout = self._redact(stdout[:self.MAX_OUTPUT_BYTES], runtime_secrets)
            stderr = self._redact(stderr[:self.MAX_OUTPUT_BYTES], runtime_secrets)
        duration_ms = round((time.monotonic() - started) * 1000)
        response = {
            "execution_id": execution_id, "skill_id": skill_id,
            "exit_code": process.returncode, "timed_out": timed_out,
            "duration_ms": duration_ms, "stdout": stdout, "stderr": stderr,
            "output_truncated": truncated,
        }
        self.store.log(user.user_id, "tool_execution", skill_id=skill_id,
                       execution_id=execution_id,
                       input_snapshot={"code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                                        "code_bytes": len(code.encode())},
                       result_preview={"exit_code": process.returncode, "timed_out": timed_out,
                                        "duration_ms": duration_ms,
                                        "stdout_bytes": len(stdout.encode()),
                                        "stderr_bytes": len(stderr.encode())},
                       tags=[f"injected_env_names:{','.join(sorted(runtime_secrets))}"])
        return {"ok": True, "data": response}


class SecretService:
    """In-memory secret registry for this local POC."""

    def __init__(self, store: Store):
        self.store = store

    def list_public(self) -> list[dict[str, Any]]:
        return [{"name": secret.name, "value": secret.value}
                for secret in self.store.secrets.values()]

    def configure(self, secret: SecretEnvVar, operator: UserContext) -> dict[str, Any]:
        if "admin" not in operator.permissions:
            return {"ok": False, "msg": "无权限配置密钥环境变量"}
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", secret.name):
            return {"ok": False, "msg": "环境变量名须符合 [A-Z_][A-Z0-9_]*"}
        if not isinstance(secret.value, str) or not secret.value or len(secret.value) > 16_384 or "\x00" in secret.value:
            return {"ok": False, "msg": "环境变量值不能为空且长度不能超过16KB"}
        self.store.secrets[secret.name] = secret
        self.store.log(operator.user_id, "secret_configure", result_preview={"name": secret.name},
                       tags=["secret_value_not_audited"])
        return {"ok": True, "data": {"name": secret.name}}

    def remove(self, name: str, operator: UserContext) -> dict[str, Any]:
        if "admin" not in operator.permissions:
            return {"ok": False, "msg": "无权限删除密钥环境变量"}
        if name not in self.store.secrets:
            return {"ok": False, "msg": "环境变量不存在"}
        del self.store.secrets[name]
        self.store.log(operator.user_id, "secret_remove", result_preview={"name": name})
        return {"ok": True, "data": {"name": name, "removed": True}}


class SkillGenerator:
    """OpenAI-compatible generator, with a deterministic local fallback for offline use."""

    @staticmethod
    def model_config() -> dict[str, Any]:
        return {"provider": "remote" if MODEL_BASE_URL and MODEL_NAME else "mock",
                "base_url": MODEL_BASE_URL, "model": MODEL_NAME,
                "api_key_configured": bool(MODEL_API_KEY)}

    @staticmethod
    def mock_generate(description: str) -> dict[str, Any]:
        weekly = any(word in description.lower() for word in ("周报", "weekly", "week report"))
        contacts = any(word in description.lower() for word in ("联系人", "通讯录", "contact list", "contacts"))
        if weekly:
            name, skill_id = "周报助手", "weekly-report-assistant"
            outline = [{"title": "本周完成", "guidance": "总结可验证的工作和结果。"},
                       {"title": "风险与问题", "guidance": "列出阻塞及需要协助的事项。"},
                       {"title": "下周计划", "guidance": "按优先级列出计划和交付物。"}]
        elif contacts:
            name, skill_id = "Custos 通讯录", "custos-contacts"
            outline = [{"title": "可用文件"}, {"title": "匹配联系人"}, {"title": "联系信息"}]
        else:
            name, skill_id = "自定义 Skill", "custom-skill"
            outline = [{"title": "任务理解"}, {"title": "执行结果"}, {"title": "待确认事项"}]
        return {
            "skill_id": skill_id, "name": name, "description": description[:180],
            "system_prompt_snippet": f"你是{name}。根据以下需求完成任务：{description}\n遵循输出大纲，不编造未提供的事实。",
            "outline": outline,
            "output_schema": {"type": "object", "required": [part["title"] for part in outline]},
            "required_env_vars": ["CUSTOS_CONTACTS_DIR"] if contacts else [],
        }

    @staticmethod
    def _decode_result(raw: bytes) -> dict[str, Any]:
        response = json.loads(raw.decode("utf-8"))
        content = response["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
        content = str(content).strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        result = json.loads(content)
        required = {"skill_id", "name", "description", "system_prompt_snippet", "outline", "output_schema"}
        if not isinstance(result, dict) or not required.issubset(result):
            raise ValueError("模型返回缺少 Skill 必需字段")
        allowed = required | {"required_env_vars"}
        result = {key: value for key, value in result.items() if key in allowed}
        result.setdefault("required_env_vars", [])
        return result

    def generate(self, description: str) -> tuple[dict[str, Any], str]:
        if not MODEL_BASE_URL or not MODEL_NAME:
            return self.mock_generate(description), "mock"
        endpoint = MODEL_BASE_URL.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        request_body = {
            "model": MODEL_NAME, "temperature": 0.2,
            "messages": [
                {"role": "system", "content": (
                    "你是企业 Skill 设计器。根据管理员的需求，生成可执行、边界明确的 Skill 配置。"
                    "只返回 JSON 对象，字段为 skill_id(小写kebab-case), name, description, system_prompt_snippet, "
                    "outline(对象数组，包含title和可选guidance), output_schema(JSON Schema对象), "
                    "required_env_vars(环境变量名数组，只填写明确需要的名称，不得填写或猜测变量值)。"
                    "不允许生成可执行代码、脚本或环境变量值。"
                )},
                {"role": "user", "content": description},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if MODEL_API_KEY:
            headers["Authorization"] = f"Bearer {MODEL_API_KEY}"
        req = Request(endpoint, data=json.dumps(request_body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(req, timeout=45) as response:
                result = self._decode_result(response.read())
        except HTTPError as exc:
            if exc.code != 400:
                raise RuntimeError(f"模型服务返回 HTTP {exc.code}") from None
            request_body.pop("response_format", None)
            req = Request(endpoint, data=json.dumps(request_body).encode("utf-8"), headers=headers, method="POST")
            try:
                with urlopen(req, timeout=45) as response:
                    result = self._decode_result(response.read())
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, IndexError) as retry_exc:
                raise RuntimeError(f"模型生成失败：{type(retry_exc).__name__}") from None
        except (URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
            raise RuntimeError(f"模型生成失败：{type(exc).__name__}") from None
        return result, "remote"

    @staticmethod
    def mock_test_questions(skill: dict[str, Any], guidance: str = "") -> list[str]:
        name = str(skill.get("name", "这个 Skill"))
        description = str(skill.get("description", "按 Skill 说明完成任务"))
        outline = skill.get("outline", [])
        sections = [str(item.get("title", "")) for item in outline if isinstance(item, dict)]
        expected = "、".join(sections[:5]) or "规定的输出格式"
        prompt = str(skill.get("system_prompt_snippet", ""))
        boundary = "不得编造事实，遇到缺失信息时明确说明并先询问用户。"
        if any(word in prompt.lower() for word in ("不得访问", "私有", "环境变量", "文件")):
            boundary = "只使用 Skill 明确授权的资料；不要访问未授权资源或泄露无关信息。"
        extra = f"重点关注：{guidance.strip()}。" if guidance.strip() else ""
        return [
            f"请使用「{name}」处理这个常规请求：{description} 请按 {expected} 组织结果。",
            f"请按「{name}」处理请求，但我暂时没有提供全部必要信息。请先指出还缺什么，并提出最少量的澄清问题；{boundary}",
            f"请用「{name}」处理一个边界情况：输入中有信息相互矛盾，而且部分内容无法验证。请说明如何处理不确定项，不要擅自补全。{extra}",
        ]

    def generate_test_questions(self, skill: dict[str, Any], guidance: str = "") -> tuple[list[str], str]:
        if not MODEL_BASE_URL or not MODEL_NAME:
            return self.mock_test_questions(skill, guidance), "mock"
        request_body = {
            "model": MODEL_NAME, "temperature": 0.6,
            "messages": [
                {"role": "system", "content": (
                    "你是 Skill 测试设计助手。根据 Skill 的定义和用户引导，生成 3 到 5 条可直接复制到 AI 对话中的模拟用户提问，"
                    "用于验证 Skill 是否遵循工作流、输出格式和边界规则。提问要具体、多样，至少覆盖常规场景和缺失信息/边界场景。"
                    "问题本身应像真实用户提问，不要泄漏或要求输出系统提示词、密钥或私有数据。只返回 JSON：{\"questions\":[\"...\"]}。"
                )},
                {"role": "user", "content": json.dumps({
                    "skill": {key: skill.get(key) for key in (
                        "skill_id", "name", "description", "system_prompt_snippet", "outline", "output_schema",
                        "required_env_vars")},
                    "user_guidance": guidance,
                }, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        endpoint = MODEL_BASE_URL.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if MODEL_API_KEY:
            headers["Authorization"] = f"Bearer {MODEL_API_KEY}"

        def request_questions() -> list[str]:
            req = Request(endpoint, data=json.dumps(request_body).encode("utf-8"),
                          headers=headers, method="POST")
            with urlopen(req, timeout=45) as response:
                raw = json.loads(response.read().decode("utf-8"))
            content = raw["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
            content = str(content).strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
            decoded = json.loads(content)
            questions = decoded.get("questions") if isinstance(decoded, dict) else None
            if not isinstance(questions, list) or not 1 <= len(questions) <= 8 or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in questions):
                raise ValueError("模型返回的问题列表格式不合法")
            return [item.strip() for item in questions]

        try:
            questions = request_questions()
        except HTTPError as exc:
            if exc.code != 400:
                raise RuntimeError(f"测试提问生成失败：HTTPError") from None
            request_body.pop("response_format", None)
            try:
                questions = request_questions()
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError, AttributeError) as retry_exc:
                raise RuntimeError(f"测试提问生成失败：{type(retry_exc).__name__}") from None
        except (URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"测试提问生成失败：{type(exc).__name__}") from None
        return questions, "remote"


GENERATOR = SkillGenerator()


def build_app() -> tuple[Store, SkillService, ToolExecutionService, SecretService]:
    store = Store()
    skills = SkillService(store)
    tool_executor = ToolExecutionService(store, skills)
    secrets = SecretService(store)
    admin = UserContext("seed-admin", permissions=["admin"])
    contacts_dir = tempfile.mkdtemp(prefix="custos-contacts-")
    atexit.register(shutil.rmtree, contacts_dir, ignore_errors=True)
    contacts_file = Path(contacts_dir) / "custos_contacts.csv"
    contacts_file.write_text(
        "name,department,role,email\n"
        "Lin Chen,Custos Platform,Platform Lead,lin.chen@custos.example\n"
        "Mei Wang,Product,Product Manager,mei.wang@custos.example\n"
        "Alex Kim,Engineering,Backend Engineer,alex.kim@custos.example\n"
        "Rui Zhang,People Operations,People Partner,rui.zhang@custos.example\n"
        "Jia Liu,Design,UX Designer,jia.liu@custos.example\n"
        "Omar Hassan,Security,Security Engineer,omar.hassan@custos.example\n"
        "Priya Patel,Data,Data Scientist,priya.patel@custos.example\n"
        "Noah Williams,Finance,Finance Analyst,noah.williams@custos.example\n"
        "Sofia Garcia,Marketing,Marketing Manager,sofia.garcia@custos.example\n"
        "Ethan Brown,Sales,Account Executive,ethan.brown@custos.example\n"
        "Yuki Tanaka,Customer Support,Support Specialist,yuki.tanaka@custos.example\n"
        "Amara Okafor,Legal,Legal Counsel,amara.okafor@custos.example\n"
        "Mateo Rossi,Quality,QA Engineer,mateo.rossi@custos.example\n"
        "Chloe Dubois,Infrastructure,DevOps Engineer,chloe.dubois@custos.example\n"
        "Arjun Mehta,AI Research,Machine Learning Engineer,arjun.mehta@custos.example\n"
        "Emma Wilson,Documentation,Technical Writer,emma.wilson@custos.example\n"
        "Diego Silva,Procurement,Procurement Specialist,diego.silva@custos.example\n"
        "Hana Suzuki,Mobile,Mobile Developer,hana.suzuki@custos.example\n"
        "Grace Lee,Operations,Operations Manager,grace.lee@custos.example\n"
        "Lucas Martin,Solutions,Solutions Architect,lucas.martin@custos.example\n",
        encoding="utf-8",
    )
    secrets.configure(SecretEnvVar(name="CUSTOS_CONTACTS_DIR", value=contacts_dir), admin)
    skills.publish(SkillMeta(
        skill_id="weekly-report", name="Custos 周报助手", description="根据用户提供的信息整理周报。", version="1.0.0",
        system_prompt_snippet="你是 Custos 周报助手。请基于用户提供的信息整理周报，不要编造事实；缺失内容明确标注待补充。最终文档第一行必须精确使用标题：Custos 周报。该标题是服务端托管模板的一部分。",
        output_schema={"type": "object", "required": ["本周完成", "风险与问题", "下周计划"]},
        outline=[{"title": "本周完成", "guidance": "概括本周完成的工作与阶段性成果。"},
                 {"title": "风险与问题", "guidance": "列出阻塞、风险和需要协助的事项；没有则注明暂无。"},
                 {"title": "下周计划", "guidance": "按优先级列出下周计划和预期交付。"}],
        ), admin)
    skills.publish(SkillMeta(
        skill_id="custos-contacts", name="Custos 通讯录", description="列出并读取 Custos 企业联系人目录。", version="1.0.0",
        system_prompt_snippet="你可以使用一次性 Python 子进程读取企业通讯录。运行时环境变量 CUSTOS_CONTACTS_DIR 是允许访问的绝对目录。需要先用 pathlib.Path(os.environ['CUSTOS_CONTACTS_DIR']).iterdir() 列出目录文件，再按需读取其中的 CSV 文件并查找联系人姓名、部门、职位或邮箱。不得访问该目录之外的文件；不得虚构或泄露与用户请求无关的联系人信息。",
        output_schema={"type": "object", "required": ["files", "contacts"]},
        outline=[{"title": "可用文件"}, {"title": "匹配联系人"}, {"title": "联系信息"}],
        required_env_vars=["CUSTOS_CONTACTS_DIR"]), admin)
    # Startup fixtures are not user actions; keep the audit view focused on runtime activity.
    store.audit.clear()
    return store, skills, tool_executor, secrets


STORE, SKILLS, TOOL_EXECUTOR, SECRETS = build_app()


def make_skill_markdown(meta: dict[str, Any]) -> str:
    name = json.dumps(meta["name"], ensure_ascii=False)
    description = json.dumps(meta["description"], ensure_ascii=False)
    return f"""---
name: {name}
description: {description}
---

# {meta['name']}

This Custos skill has server-managed runtime instructions that may change without reinstalling this file.

## Required workflow

1. At the start of each task, call the `get_skill_config` tool on the `custos` MCP server with `skill_id: {meta['skill_id']}`.
2. Follow the latest `system_prompt_snippet`, `outline`, and `output_schema` returned by the service. Do not rely on an old copy of those settings.
3. Use only the current `required_env_vars` returned by `get_skill_config` to request environment variables; the bundle-time list below may be stale after a server-side update. Secret values are resolved by the service and must never be requested from the user or printed.
4. If the task needs generated code or a script, call the generic `run_code` tool on the `custos` MCP server with this `skill_id`. This prototype runs code in a temporary child process; it is not a sandbox.
5. If the service rejects access or execution, stop and explain the denial; do not access the resource through another route.

## Current platform configuration

- Skill ID: `{meta['skill_id']}`
- Downloaded bundle version: `{meta['version']}`
- Bundle-time required environment variable names: `{', '.join(meta.get('required_env_vars', [])) or 'none'}` (for reference only; fetch current config at runtime)
- Current detailed prompt/configuration: fetched at runtime from the Custos service.
"""


def user_from_headers(handler: BaseHTTPRequestHandler) -> UserContext:
    return UserContext(
        user_id=handler.headers.get("X-User-ID", "local-user"),
        token=handler.headers.get("Authorization", "mock-token"),
        permissions=["user"],
    )


def workbuddy_bundle(meta: dict[str, Any], service_url: str, user: UserContext,
                     harness: str = "workbuddy") -> bytes:
    if harness != "workbuddy":
        raise ValueError("当前仅支持 WorkBuddy harness")
    bundle = BytesIO()
    config = {
        "mcpServers": {"custos": {
            "type": "streamableHttp", "url": f"{service_url.rstrip('/')}/mcp", "timeout": 30000,
            "headers": {
                "X-User-ID": user.user_id,
            },
        }},
    }
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        skill_markdown = make_skill_markdown(meta)
        archive.writestr("mcp.json", json.dumps(config, ensure_ascii=False, indent=2))
        archive.writestr(f"skills/{meta['skill_id']}/SKILL.md", skill_markdown)
        archive.writestr("SKILL.md", skill_markdown)
        archive.writestr("CUSTOS-SETUP.txt", (
            "Import the skills/<skill-id>/ folder into WorkBuddy's Skills area (or copy it to ~/.workbuddy/skills/).\n"
            "Configure the included mcp.json in WorkBuddy's MCP settings to connect Custos for managed Skill configuration and tool execution.\n"
            "SKILL.md at the archive root is a visible copy; the active skill is under skills/<skill-id>/.\n"
            "This local mock platform does not authenticate identities; its MCP headers only simulate user context.\n"
        ))
    return bundle.getvalue()


def open_code_bundle(meta: dict[str, Any], service_url: str, user: UserContext) -> bytes:
    bundle = BytesIO()
    config = {
        "$schema": "https://opencode.ai/config.json",
        "skills": ["./.opencode/skills"],
        "mcp": {"servers": {"custos": {
            "type": "remote", "url": f"{service_url.rstrip('/')}/mcp", "oauth": False,
            "headers": {"X-User-ID": user.user_id},
        }}},
    }
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        skill_markdown = make_skill_markdown(meta)
        archive.writestr("opencode.jsonc", json.dumps(config, ensure_ascii=False, indent=2))
        archive.writestr(f".opencode/skills/{meta['skill_id']}/SKILL.md", skill_markdown)
        archive.writestr("SKILL.md", skill_markdown)
        archive.writestr("CUSTOS-SETUP.txt", (
            "Extract this archive into an OpenCode project directory.\n"
            "The included OpenCode config connects the Custos MCP server for managed Skill configuration and tool execution.\n"
            "The active skill is under .opencode/skills/.\n"
            "This local mock platform does not authenticate identities; its MCP headers only simulate user context.\n"
        ))
    return bundle.getvalue()


def mcp_tool_catalog() -> list[dict[str, Any]]:
    return [
        {"name": "get_skill_config",
         "description": "Fetch the latest published prompt, template, output schema, and required environment variable names for a managed Skill.",
         "inputSchema": {"type": "object", "properties": {"skill_id": {"type": "string"}},
                         "required": ["skill_id"], "additionalProperties": False}},
        {"name": "run_code",
         "description": "Run temporary Python code in a child process for this prototype. This is not a sandbox. Skill-declared server variables are injected for this execution only.",
         "inputSchema": {"type": "object", "properties": {
             "skill_id": {"type": "string"},
             "code": {"type": "string", "description": "Python source code, maximum 64KB."}},
             "required": ["skill_id", "code"], "additionalProperties": False}},
    ]


def mcp_call(name: str, arguments: dict[str, Any], user: UserContext) -> dict[str, Any]:
    skill_id = str(arguments.get("skill_id", ""))
    if name == "get_skill_config":
        result = SKILLS.effective(skill_id, user, audit=False)
        if not result["ok"]:
            response = {"isError": True, "content": [{"type": "text", "text": result["msg"]}]}
        else:
            meta = result["data"]
            data = {
                "skill_id": meta["skill_id"], "name": meta["name"],
                "version": meta["version"], "system_prompt_snippet": meta["system_prompt_snippet"],
                "outline": meta["outline"], "output_schema": meta["output_schema"],
                "required_env_vars": meta.get("required_env_vars", []),
            }
            response = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                        "structuredContent": data}
    elif name == "run_code":
        result = TOOL_EXECUTOR.run(skill_id, arguments.get("code", ""), user)
        if not result["ok"]:
            response = {"isError": True, "content": [{"type": "text", "text": result["msg"]}]}
        else:
            data = result["data"]
            response = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                        "structuredContent": data}
            if data["exit_code"] != 0 or data["timed_out"]:
                response["isError"] = True
    else:
        response = {"isError": True, "content": [{"type": "text", "text": "MCP tool not found"}]}

    structured = response.get("structuredContent", {})
    if name == "run_code" and structured:
        preview = {key: structured.get(key) for key in
                   ("execution_id", "exit_code", "timed_out", "duration_ms")}
        code = arguments.get("code", "")
        input_snapshot = {"skill_id": skill_id,
                          "code_sha256": hashlib.sha256(code.encode()).hexdigest() if isinstance(code, str) else None,
                          "code_bytes": len(code.encode()) if isinstance(code, str) else None}
    else:
        preview = {"version": structured.get("version")} if structured else {}
        input_snapshot = {"skill_id": skill_id} if skill_id else {}
    STORE.log(user.user_id, "tool_call", skill_id=skill_id or None,
              execution_id=structured.get("execution_id"), tool_name=name,
              input_snapshot=input_snapshot,
              result_preview={"is_error": bool(response.get("isError")), **preview})
    return response


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "CustosMock/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, body: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求体不能超过 1MB")
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("JSON请求体必须是对象")
        return body

    @staticmethod
    def _context(value: Any = None, default: UserContext | None = None) -> UserContext:
        return UserContext.from_dict(value, default)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path == "/":
            try:
                page = (Path(__file__).parent / "static" / "index.html").read_bytes()
            except OSError:
                return self._send({"ok": False, "msg": "前端页面文件不可用"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            return self.wfile.write(page)
        if path in {"/secrets-page", "/runtime-logs-page"}:
            filename = {"/secrets-page": "secrets.html", "/runtime-logs-page": "runtime-logs.html"}[path]
            try:
                page = (Path(__file__).parent / "static" / filename).read_bytes()
            except OSError:
                return self._send({"ok": False, "msg": "前端页面文件不可用"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            return self.wfile.write(page)
        if path == "/health":
            return self._send({"ok": True, "service": "custos-mock"})
        if path == "/model/config":
            return self._send({"ok": True, "data": GENERATOR.model_config()})
        if path == "/skills":
            return self._send({"ok": True, "data": [asdict(item) for item in SKILLS.latest_versions()]})
        if path == "/secrets":
            return self._send({"ok": True, "data": SECRETS.list_public()})
        if path == "/runtime-logs":
            records = [row for row in STORE.audit if row["action_type"] == "tool_call"]
            if query.get("tool_name"):
                records = [row for row in records if row.get("tool_name") == query["tool_name"][0]]
            if query.get("skill_id"):
                records = [row for row in records if row.get("skill_id") == query["skill_id"][0]]
            try:
                limit = max(1, min(1000, int(query.get("limit", ["200"])[0])))
            except ValueError:
                return self._send({"ok": False, "msg": "limit必须是整数"}, 400)
            return self._send({"ok": True, "data": list(reversed(records[-limit:]))})
        config_match = re.fullmatch(r"/skill/([^/]+)/config", path)
        if config_match:
            skill_id = unquote(config_match.group(1))
            try:
                user = UserContext.from_dict({
                    "user_id": query.get("user_id", ["local-user"])[0],
                })
                result = SKILLS.effective(skill_id, user)
                if not result["ok"]:
                    return self._send(result, 403 if "禁止" in result["msg"] or "不允许" in result["msg"] else 404)
                meta = result["data"]
                return self._send({"ok": True, "data": {
                    "skill_id": meta["skill_id"], "name": meta["name"], "version": meta["version"],
                    "system_prompt_snippet": meta["system_prompt_snippet"],
                    "output_schema": meta["output_schema"], "outline": meta["outline"],
                    "required_env_vars": meta.get("required_env_vars", []),
                }})
            except (ValueError, TypeError) as exc:
                return self._send({"ok": False, "msg": str(exc)}, 400)
        download_match = re.fullmatch(r"/skill/([^/]+)/download", path)
        if download_match:
            skill_id = unquote(download_match.group(1))
            harness = query.get("harness", ["workbuddy"])[0]
            if harness not in {"workbuddy", "opencode"}:
                return self._send({"ok": False, "msg": "不支持该 Harness，可选 WorkBuddy 或 OpenCode"}, 400)
            try:
                user = UserContext.from_dict({
                    "user_id": query.get("user_id", ["local-user"])[0],
                })
                result = SKILLS.effective(skill_id, user)
                if not result["ok"]:
                    return self._send(result, 403 if "禁止" in result["msg"] or "不允许" in result["msg"] else 404)
                host = self.headers.get("X-Forwarded-Host", self.headers.get("Host", "127.0.0.1:8000"))
                scheme = self.headers.get("X-Forwarded-Proto", "http")
                service_url = f"{scheme}://{host}"
                archive = (workbuddy_bundle(result["data"], service_url, user) if harness == "workbuddy"
                           else open_code_bundle(result["data"], service_url, user))
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{skill_id}-{harness}.zip"')
                self.send_header("Content-Length", str(len(archive)))
                self.end_headers()
                return self.wfile.write(archive)
            except (ValueError, TypeError) as exc:
                return self._send({"ok": False, "msg": str(exc)}, 400)
        match = re.fullmatch(r"/skill/([^/]+)/effective", path)
        if match:
            try:
                user = UserContext.from_dict({
                    "user_id": query.get("user_id", [None])[0],
                    "permissions": query.get("permissions", ["user"])[0].split(","),
                })
                return self._send(SKILLS.effective(unquote(match.group(1)), user))
            except (ValueError, TypeError) as exc:
                return self._send({"ok": False, "msg": str(exc)}, 400)
        if path == "/audit/query":
            records = list(STORE.audit)
            if query.get("user_id"):
                records = [row for row in records if row["user_id"] == query["user_id"][0]]
            if query.get("action_type"):
                records = [row for row in records if row["action_type"] == query["action_type"][0]]
            try:
                limit = max(1, min(1000, int(query.get("limit", ["100"])[0])))
            except ValueError:
                return self._send({"ok": False, "msg": "limit必须是整数"}, 400)
            return self._send({"ok": True, "data": records[-limit:]})
        return self._send({"ok": False, "msg": "接口不存在"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/mcp":
            return self._handle_mcp()
        try:
            body = self._body()
            if path == "/skill/generate":
                operator = self._context(body.get("operator"))
                if "admin" not in operator.permissions:
                    return self._send({"ok": False, "msg": "无权限生成skill"}, 403)
                description = str(body.get("description", "")).strip()
                if not description or len(description) > 8000:
                    return self._send({"ok": False, "msg": "需求描述不能为空且不能超过8000字"}, 400)
                try:
                    generated, generation_mode = GENERATOR.generate(description)
                except RuntimeError as exc:
                    return self._send({"ok": False, "msg": str(exc)}, 502)
                STORE.log(operator.user_id, "skill_generate",
                          input_snapshot={"description_sha256": hashlib.sha256(description.encode()).hexdigest()},
                          result_preview={"skill_id": generated.get("skill_id"), "mode": generation_mode})
                return self._send({"ok": True, "data": generated, "generation_mode": generation_mode})
            if path == "/skill/test-questions":
                operator = self._context(body.get("operator"))
                if "admin" not in operator.permissions:
                    return self._send({"ok": False, "msg": "无权限生成测试提问"}, 403)
                skill_id = str(body.get("skill_id", "")).strip()
                guidance = str(body.get("guidance", "")).strip()
                if not skill_id or len(guidance) > 2000:
                    return self._send({"ok": False, "msg": "Skill ID 不能为空，引导不能超过2000字"}, 400)
                versions = STORE.skills.get(skill_id, [])
                if not versions:
                    return self._send({"ok": False, "msg": "skill不存在"}, 404)
                skill = asdict(max(versions, key=lambda item: item.update_at))
                try:
                    questions, generation_mode = GENERATOR.generate_test_questions(skill, guidance)
                except RuntimeError as exc:
                    return self._send({"ok": False, "msg": str(exc)}, 502)
                STORE.log(operator.user_id, "skill_test_questions_generate", skill_id=skill_id,
                          input_snapshot={"guidance_sha256": hashlib.sha256(guidance.encode()).hexdigest()},
                          result_preview={"question_count": len(questions), "mode": generation_mode})
                return self._send({"ok": True, "data": {"questions": questions},
                                   "generation_mode": generation_mode})
            if path == "/skill/publish":
                operator = self._context(body.get("operator"))
                meta = SkillMeta(**body["skill"])
                return self._send(SKILLS.publish(meta, operator))
            if path == "/skill/rollback":
                operator = self._context(body.get("operator"))
                return self._send(SKILLS.rollback(str(body["skill_id"]), str(body["target_version"]), operator))
            if path == "/secrets/configure":
                operator = self._context(body.get("operator"))
                return self._send(SECRETS.configure(SecretEnvVar(**body["secret"]), operator))
            if path == "/secrets/remove":
                operator = self._context(body.get("operator"))
                return self._send(SECRETS.remove(str(body.get("name", "")), operator))
            return self._send({"ok": False, "msg": "接口不存在"}, 404)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._send({"ok": False, "msg": f"请求格式错误：{exc}"}, 400)

    def _handle_mcp(self) -> None:
        try:
            request = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": f"Parse error: {exc}"}}, 400)
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict):
            return self._send({"jsonrpc": "2.0", "id": request_id,
                               "error": {"code": -32600, "message": "Invalid Request"}}, 400)
        if method.startswith("notifications/"):
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "custos", "version": "0.1.0"},
        "instructions": "Managed Skill config is fetched for each task. run_code executes temporary generated code in a child process for this prototype. Only Skill-declared server environment variables are injected for that run. This is not a sandbox or security boundary.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": mcp_tool_catalog()}
        elif method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                result = {"isError": True, "content": [{"type": "text", "text": "arguments must be an object"}]}
            else:
                result = mcp_call(str(name), arguments, user_from_headers(self))
        else:
            return self._send({"jsonrpc": "2.0", "id": request_id,
                               "error": {"code": -32601, "message": "Method not found"}}, 200)
        return self._send({"jsonrpc": "2.0", "id": request_id, "result": result}, 200)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Custos 单进程 Mock 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"Custos mock service listening on http://{args.host}:{args.port}")
    print(f"Open http://{args.host}:{args.port}/ for the UI; GET /audit/query for audit records")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
