import json
import csv
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from app import (GENERATOR, SECRETS, SKILLS, STORE, TOOL_EXECUTOR,
                 SecretEnvVar, SkillMeta, UserContext,
                 build_app, mcp_call, open_code_bundle, workbuddy_bundle)


class CustosTests(unittest.TestCase):
    def setUp(self):
        STORE.audit.clear()

    def test_startup_examples_are_weekly_report_and_private_contacts(self):
        fresh_store, fresh_skills, _, _ = build_app()
        self.assertEqual({skill.skill_id for skill in fresh_skills.list_all()},
                         {"weekly-report", "custos-contacts"})
        weekly = fresh_skills.effective("weekly-report", UserContext("staff"))["data"]
        self.assertIn("Custos 周报", weekly["system_prompt_snippet"])
        self.assertNotIn("secret_level", weekly)
        contacts = Path(fresh_store.secrets["CUSTOS_CONTACTS_DIR"].value)
        self.assertTrue(contacts.is_absolute())
        self.assertTrue((contacts / "custos_contacts.csv").is_file())
        with (contacts / "custos_contacts.csv").open(encoding="utf-8", newline="") as source:
            people = list(csv.DictReader(source))
        self.assertEqual(len(people), 20)
        self.assertGreaterEqual(len({person["role"] for person in people}), 18)

    def test_contact_skill_lists_and_reads_startup_temp_directory(self):
        code = (
            "import os\nfrom pathlib import Path\n"
            "root=Path(os.environ['CUSTOS_CONTACTS_DIR'])\n"
            "print('FILES:', ','.join(sorted(p.name for p in root.iterdir() if p.is_file())))\n"
            "print((root/'custos_contacts.csv').read_text())\n"
        )
        result = TOOL_EXECUTOR.run("custos-contacts", code, UserContext("alice"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["exit_code"], 0)
        self.assertIn("custos_contacts.csv", result["data"]["stdout"])
        self.assertIn("lin.chen@custos.example", result["data"]["stdout"])
        self.assertIn("injected_env_names:CUSTOS_CONTACTS_DIR", STORE.audit[-1]["tags"])

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
        self.assertFalse(any(row["action_type"] == "tool_execution" for row in STORE.audit))

    def test_publishing_same_skill_replaces_the_current_config(self):
        _, fresh_skills, _, _ = build_app()
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
        current = fresh_skills.effective("replace-me", UserContext("viewer"))["data"]
        self.assertEqual(current["system_prompt_snippet"], "updated prompt")
        self.assertEqual(sum(skill.skill_id == "replace-me" for skill in fresh_skills.list_all()), 1)

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

    def test_environment_variable_values_are_visible_but_not_audited(self):
        value = "/private/path/not-for-response"
        result = SECRETS.configure(
            SecretEnvVar("EXAMPLE_PATH", value),
            UserContext("admin", permissions=["admin"]),
        )
        self.assertTrue(result["ok"])
        visible = next(item for item in SECRETS.list_public() if item["name"] == "EXAMPLE_PATH")
        self.assertEqual(visible["value"], value)
        self.assertNotIn(value, json.dumps(result))
        self.assertNotIn(value, json.dumps(STORE.audit))

    def test_mcp_skill_config_and_generic_tool_runner(self):
        user = UserContext("alice")
        config = mcp_call("get_skill_config", {"skill_id": "custos-contacts"}, user)
        self.assertEqual(config["structuredContent"]["required_env_vars"], ["CUSTOS_CONTACTS_DIR"])
        self.assertNotIn("version", config["structuredContent"])
        result = mcp_call("run_code", {
            "skill_id": "custos-contacts",
            "code": "import os; print(os.path.isabs(os.environ['CUSTOS_CONTACTS_DIR']))",
        }, user)
        self.assertFalse(result.get("isError", False))
        self.assertIn("True", result["structuredContent"]["stdout"])

    def test_workbuddy_bundle_contains_skill_and_mcp_config(self):
        weekly = SKILLS.effective("weekly-report", UserContext("alice"))["data"]
        self.assertIn("Custos 周报", weekly["system_prompt_snippet"])
        self.assertNotIn("gray_teams", weekly)
        self.assertNotIn("version", weekly)
        bundle = workbuddy_bundle(weekly, "http://localhost:8000", UserContext("alice"))
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            skill = archive.read("skills/weekly-report/SKILL.md").decode()
            visible_skill = archive.read("SKILL.md").decode()
            config = json.loads(archive.read("mcp.json"))
        self.assertNotIn("该标题是服务端托管模板的一部分", skill)
        self.assertNotIn("该标题是服务端托管模板的一部分", visible_skill)
        self.assertEqual(skill, visible_skill)
        self.assertNotIn("Downloaded bundle version", skill)
        self.assertEqual(config["mcpServers"]["custos"]["url"], "http://localhost:8000/mcp")
        self.assertEqual(config["mcpServers"]["custos"]["type"], "streamableHttp")
        self.assertNotIn("X-Team-ID", json.dumps(config))
        self.assertNotIn("X-Model-Bypass", json.dumps(config))

    def test_opencode_bundle_contains_opencode_skill_and_mcp_config(self):
        weekly = SKILLS.effective("weekly-report", UserContext("alice"))["data"]
        bundle = open_code_bundle(weekly, "http://localhost:8000", UserContext("alice"))
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            skill = archive.read(".opencode/skills/weekly-report/SKILL.md").decode()
            config = json.loads(archive.read("opencode.jsonc"))
        self.assertNotIn("该标题是服务端托管模板的一部分", skill)
        self.assertEqual(config["mcp"]["servers"]["custos"]["url"], "http://localhost:8000/mcp")

    def test_mock_skill_generation_handles_contacts(self):
        generated = GENERATOR.mock_generate("创建查看 Custos 联系人列表和读取邮箱的 Skill")
        self.assertEqual(generated["required_env_vars"], ["CUSTOS_CONTACTS_DIR"])
        self.assertEqual(generated["skill_id"], "custos-contacts")
        self.assertNotIn("secret_level", generated)

    def test_mock_test_questions_use_skill_definition_and_guidance(self):
        skill = SKILLS.effective("weekly-report", UserContext("alice"))["data"]
        questions, mode = GENERATOR.generate_test_questions(skill, "重点测试缺失信息")
        self.assertEqual(mode, "mock")
        self.assertEqual(len(questions), 3)
        self.assertTrue(any("周报" in question for question in questions))
        self.assertTrue(any("缺失信息" in question for question in questions))

    def test_mcp_tool_calls_are_logged_without_recording_code(self):
        user = UserContext("alice")
        mcp_call("get_skill_config", {"skill_id": "custos-contacts"}, user)
        source = "print('do not store source')"
        result = mcp_call("run_code", {"skill_id": "custos-contacts", "code": source}, user)
        self.assertFalse(result.get("isError", False))
        tool_calls = [row for row in STORE.audit if row["action_type"] == "tool_call"]
        self.assertEqual([row["tool_name"] for row in tool_calls], ["get_skill_config", "run_code"])
        run_call = tool_calls[-1]
        self.assertNotIn(source, json.dumps(run_call))
        self.assertEqual(run_call["input_snapshot"]["code_bytes"], len(source.encode()))


if __name__ == "__main__":
    unittest.main()
