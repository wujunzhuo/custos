import json
import csv
import re
import threading
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from http.server import ThreadingHTTPServer

from app import (GENERATOR, OBSERVABILITY, SECRETS, SKILLS, TOOL_EXECUTOR,
                 RequestHandler, SecretEnvVar, SkillMeta, UserContext,
                 build_app, mcp_call, open_code_bundle, user_from_headers,
                 workbuddy_bundle)


class CustosTests(unittest.TestCase):
    def setUp(self):
        OBSERVABILITY.clear()

    def test_startup_examples_are_weekly_report_and_private_contacts(self):
        fresh_store, fresh_skills, _, _, _ = build_app()
        self.assertEqual({skill.skill_id for skill in fresh_skills.list_all()},
                         {"weekly-report", "custos-contacts"})
        weekly = fresh_skills.effective("weekly-report")["data"]
        self.assertIn("Custos 周报", weekly["system_prompt_snippet"])
        self.assertNotIn("secret_level", weekly)
        contacts = Path(fresh_store.secrets["CUSTOS_CONTACTS_CSV"].value)
        self.assertTrue(contacts.is_absolute())
        self.assertTrue(contacts.is_file())
        with contacts.open(encoding="utf-8", newline="") as source:
            people = list(csv.DictReader(source))
        self.assertEqual(len(people), 20)
        self.assertGreaterEqual(len({person["role"] for person in people}), 18)

    def test_contact_skill_reads_startup_temp_csv_file(self):
        code = (
            "import os\nfrom pathlib import Path\n"
            "contacts=Path(os.environ['CUSTOS_CONTACTS_CSV'])\n"
            "print(contacts.name)\n"
            "print(contacts.read_text())\n"
        )
        result = TOOL_EXECUTOR.run("custos-contacts", code, UserContext("alice"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["exit_code"], 0)
        self.assertIn("custos-contacts-", result["data"]["stdout"])
        self.assertIn("lin.chen@custos.example", result["data"]["stdout"])
        self.assertIn("injected_env_names:CUSTOS_CONTACTS_CSV", OBSERVABILITY.events[-1]["tags"])

    def test_unrequested_environment_variables_are_not_injected(self):
        admin = UserContext("admin", permissions=["admin"])
        SECRETS.configure(SecretEnvVar("UNREQUESTED_VALUE", "never-inject-this"), admin)
        try:
            result = TOOL_EXECUTOR.run(
                "custos-contacts",
                "import os; print(bool(os.getenv('UNREQUESTED_VALUE')))",
                UserContext("alice"),
            )
        finally:
            SECRETS.remove("UNREQUESTED_VALUE", admin)
        self.assertTrue(result["ok"])
        self.assertIn("False", result["data"]["stdout"])

    def test_missing_required_environment_is_rejected(self):
        admin = UserContext("admin", permissions=["admin"])
        meta = SkillMeta(
            skill_id="missing-env-test", name="Missing env test", description="test required env",
            system_prompt_snippet="test",
            output_schema={}, outline=[], required_env_vars=["NOT_CONFIGURED"],
        )
        self.assertTrue(SKILLS.publish(meta, admin)["ok"])
        missing = TOOL_EXECUTOR.run("missing-env-test", "print('should not start')",
                                    UserContext("alice"))
        self.assertFalse(missing["ok"])
        self.assertIn("NOT_CONFIGURED", missing["msg"])
        self.assertFalse(any(row["event_type"] == "tool_execution" for row in OBSERVABILITY.events))

    def test_publishing_same_skill_replaces_the_current_config(self):
        _, fresh_skills, _, _, _ = build_app()
        admin = UserContext("admin", permissions=["admin"])
        original = SkillMeta(
            skill_id="replace-me", name="First", description="First config",
            system_prompt_snippet="first prompt", output_schema={}, outline=[],
        )
        updated = SkillMeta(
            skill_id="replace-me", name="Updated", description="Updated config",
            system_prompt_snippet="updated prompt", output_schema={}, outline=[],
        )
        self.assertTrue(fresh_skills.publish(original, admin)["ok"])
        self.assertTrue(fresh_skills.publish(updated, admin)["ok"])
        current = fresh_skills.effective("replace-me")["data"]
        self.assertEqual(current["system_prompt_snippet"], "updated prompt")
        self.assertEqual(sum(skill.skill_id == "replace-me" for skill in fresh_skills.list_all()), 1)

    def test_management_changes_do_not_create_runtime_observability_events(self):
        admin = UserContext("admin", permissions=["admin"])
        meta = SkillMeta(
            skill_id="management-events-test", name="Management events", description="management activity",
            system_prompt_snippet="test", output_schema={}, outline=[],
        )
        self.assertTrue(SKILLS.publish(meta, admin)["ok"])
        self.assertTrue(SECRETS.configure(SecretEnvVar("UNLOGGED_TEST", "value"), admin)["ok"])
        self.assertTrue(SECRETS.remove("UNLOGGED_TEST", admin)["ok"])
        self.assertEqual(OBSERVABILITY.events, [])

    def test_tool_timeout_stops_child_process(self):
        admin = UserContext("admin", permissions=["admin"])
        meta = SkillMeta(
            skill_id="timeout-test-skill", name="Timeout test", description="test timeout",
            system_prompt_snippet="test",
            output_schema={}, outline=[],
        )
        self.assertTrue(SKILLS.publish(meta, admin)["ok"])
        with patch.object(TOOL_EXECUTOR, "TIMEOUT_SECONDS", 1):
            result = TOOL_EXECUTOR.run("timeout-test-skill", "while True: pass",
                                       UserContext("alice"))
        self.assertTrue(result["data"]["timed_out"])

    def test_environment_variable_values_are_not_written_to_runtime_observability(self):
        value = "/private/path/not-for-response"
        result = SECRETS.configure(
            SecretEnvVar("EXAMPLE_PATH", value),
            UserContext("admin", permissions=["admin"]),
        )
        self.assertTrue(result["ok"])
        visible = next(item for item in SECRETS.list_public() if item["name"] == "EXAMPLE_PATH")
        self.assertEqual(visible["value"], value)
        self.assertNotIn(value, json.dumps(result))
        self.assertNotIn(value, json.dumps(OBSERVABILITY.events))

    def test_mcp_skill_config_and_generic_tool_runner(self):
        user = UserContext("alice")
        config = mcp_call("get_skill_config", {"skill_id": "custos-contacts"}, user)
        self.assertEqual(config["structuredContent"]["required_env_vars"], ["CUSTOS_CONTACTS_CSV"])
        self.assertNotIn("version", config["structuredContent"])
        result = mcp_call("run_code", {
            "skill_id": "custos-contacts",
            "code": "import os; print(os.path.isfile(os.environ['CUSTOS_CONTACTS_CSV']))",
        }, user)
        self.assertFalse(result.get("isError", False))
        self.assertIn("True", result["structuredContent"]["stdout"])

    def test_mock_mcp_key_format_maps_last_six_chars_to_user_id(self):
        key = "csk_0123456789abcdef0123456789abcdef"
        user = user_from_headers(SimpleNamespace(headers={"Authorization": f"Bearer {key}"}))
        self.assertEqual(user.user_id, key[-6:])
        for authorization in ("", "Basic " + key, "Bearer invalid", "Bearer csk_" + "A" * 32):
            with self.subTest(authorization=authorization):
                with self.assertRaises(ValueError):
                    user_from_headers(SimpleNamespace(headers={"Authorization": authorization}))

    def test_mcp_tool_call_observability_uses_key_suffix_user_id(self):
        key = "csk_0123456789abcdef0123456789abcdef"
        user = user_from_headers(SimpleNamespace(headers={"Authorization": f"Bearer {key}"}))
        result = mcp_call("get_skill_config", {"skill_id": "custos-contacts"}, user)
        self.assertFalse(result.get("isError", False))
        self.assertEqual(OBSERVABILITY.events[-1]["user_id"], key[-6:])

    def test_http_mcp_requires_key_and_logs_key_suffix(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_skill_config", "arguments": {"skill_id": "weekly-report"}},
        }).encode()
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(endpoint, data=body, headers={"Content-Type": "application/json"}))
            self.assertEqual(error.exception.code, 401)
            self.assertIn("Bearer", error.exception.headers["WWW-Authenticate"])
            error.exception.close()

            key = "csk_0123456789abcdef0123456789abcdef"
            request = Request(endpoint, data=body, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {key}",
            })
            with urlopen(request) as response:
                result = json.loads(response.read())
            self.assertEqual(result["result"]["structuredContent"]["skill_id"], "weekly-report")
            self.assertEqual(OBSERVABILITY.events[-1]["user_id"], key[-6:])

            with self.assertRaises(HTTPError) as removed_route:
                urlopen(f"http://127.0.0.1:{server.server_port}/audit/query")
            self.assertEqual(removed_route.exception.code, 404)
            removed_route.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_workbuddy_bundle_contains_skill_and_inline_mcp_config_example(self):
        weekly = SKILLS.effective("weekly-report")["data"]
        self.assertIn("Custos 周报", weekly["system_prompt_snippet"])
        self.assertNotIn("gray_teams", weekly)
        self.assertNotIn("version", weekly)
        bundle = workbuddy_bundle(weekly, "http://localhost:8000")
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            skill = archive.read("skills/weekly-report/SKILL.md").decode()
            visible_skill = archive.read("SKILL.md").decode()
            setup = archive.read("CUSTOS-SETUP.txt").decode()
            filenames = set(archive.namelist())
        self.assertNotIn("该标题是服务端托管模板的一部分", skill)
        self.assertNotIn("该标题是服务端托管模板的一部分", visible_skill)
        self.assertEqual(skill, visible_skill)
        self.assertNotIn("Downloaded bundle version", skill)
        self.assertNotIn("mcp.json", filenames)
        self.assertIn('"url": "http://localhost:8000/mcp"', setup)
        self.assertIn('"Authorization": "Bearer REPLACE_WITH_YOUR_PERSONAL_KEY"', setup)
        self.assertIsNone(re.search(r"csk_[0-9a-f]{32}", setup))
        self.assertNotIn("connector-meta.json", filenames)
        self.assertNotIn("token-schema.json", filenames)
        self.assertNotIn("icon.svg", filenames)
        self.assertNotIn("X-Team-ID", setup)
        self.assertNotIn("X-Model-Bypass", setup)
        self.assertNotIn("X-User-ID", setup)

    def test_opencode_bundle_contains_opencode_skill_and_mcp_config(self):
        weekly = SKILLS.effective("weekly-report")["data"]
        bundle = open_code_bundle(weekly, "http://localhost:8000")
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            skill = archive.read(".opencode/skills/weekly-report/SKILL.md").decode()
            config = json.loads(archive.read("opencode.jsonc"))
        self.assertNotIn("该标题是服务端托管模板的一部分", skill)
        self.assertEqual(config["mcp"]["servers"]["custos"]["url"], "http://localhost:8000/mcp")
        self.assertEqual(config["mcp"]["servers"]["custos"]["headers"]["Authorization"],
                         "Bearer {env:CUSTOS_MCP_KEY}")
        self.assertFalse(config["mcp"]["servers"]["custos"]["oauth"])
        self.assertNotIn("csk_", json.dumps(config))

    def test_mock_skill_generation_handles_contacts(self):
        generated = GENERATOR.mock_generate("创建查看 Custos 联系人列表和读取邮箱的 Skill")
        self.assertEqual(generated["required_env_vars"], ["CUSTOS_CONTACTS_CSV"])
        self.assertEqual(generated["skill_id"], "custos-contacts")
        self.assertNotIn("secret_level", generated)

    def test_mock_test_questions_use_skill_definition_and_guidance(self):
        skill = SKILLS.effective("weekly-report")["data"]
        questions, mode = GENERATOR.generate_test_questions(skill, "重点测试缺失信息")
        self.assertEqual(mode, "mock")
        self.assertEqual(len(questions), 3)
        self.assertTrue(any("周报" in question for question in questions))
        self.assertTrue(any("缺失信息" in question for question in questions))

    def test_mcp_tool_calls_are_observed_without_recording_code(self):
        user = UserContext("alice")
        mcp_call("get_skill_config", {"skill_id": "custos-contacts"}, user)
        source = "print('do not store source')"
        result = mcp_call("run_code", {"skill_id": "custos-contacts", "code": source}, user)
        self.assertFalse(result.get("isError", False))
        tool_calls = [row for row in OBSERVABILITY.events if row["event_type"] == "mcp_tool_call"]
        self.assertEqual([row["tool_name"] for row in tool_calls], ["get_skill_config", "run_code"])
        run_call = tool_calls[-1]
        self.assertNotIn(source, json.dumps(run_call))
        self.assertEqual(run_call["input_snapshot"]["code_bytes"], len(source.encode()))
        self.assertTrue(any(row["event_type"] == "tool_execution" for row in OBSERVABILITY.events))


if __name__ == "__main__":
    unittest.main()
