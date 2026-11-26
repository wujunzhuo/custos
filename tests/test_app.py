import json
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from app import (GENERATOR, RUNTIME_CONFIG, SANDBOXES, SECRETS, SKILLS, STORE,
                 RuntimeConfig, SecretEnvVar, SkillMeta, UserContext,
                 build_app, mcp_call, workbuddy_bundle)


class CustosTests(unittest.TestCase):
    def setUp(self):
        STORE.audit.clear()

    def test_startup_examples_are_weekly_report_and_private_contacts(self):
        fresh_store, fresh_skills, _, _, _ = build_app()
        self.assertEqual({skill.skill_id for skill in fresh_skills.latest_versions()},
                         {"weekly-report", "custos-team-contacts"})
        weekly = fresh_skills.effective("weekly-report", UserContext("staff", "custos-team"))["data"]
        self.assertIn("Custos 团队周报", weekly["system_prompt_snippet"])
        contacts = Path(fresh_store.secrets["CUSTOS_CONTACTS_DIR"].value)
        self.assertTrue(contacts.is_absolute())
        self.assertTrue((contacts / "custos_team_contacts.csv").is_file())

    def test_contact_skill_lists_and_reads_startup_temp_directory(self):
        code = (
            "import os\nfrom pathlib import Path\n"
            "root=Path(os.environ['CUSTOS_CONTACTS_DIR'])\n"
            "print('FILES:', ','.join(sorted(p.name for p in root.iterdir() if p.is_file())))\n"
            "print((root/'custos_team_contacts.csv').read_text())\n"
        )
        result = SANDBOXES.run("custos-team-contacts", code, UserContext("alice", "custos-team"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["exit_code"], 0)
        self.assertIn("custos_team_contacts.csv", result["data"]["stdout"])
        self.assertIn("lin.chen@custos.example", result["data"]["stdout"])
        self.assertIn("injected_env_names:CUSTOS_CONTACTS_DIR", STORE.audit[-1]["tags"])

    def test_unrequested_environment_variables_are_not_injected(self):
        admin = UserContext("admin", "platform", permissions=["admin"])
        SECRETS.configure(SecretEnvVar("UNREQUESTED_VALUE", "never-inject-this", []), admin)
        try:
            result = SANDBOXES.run(
                "custos-team-contacts",
                "import os; print(bool(os.getenv('UNREQUESTED_VALUE')))",
                UserContext("alice", "custos-team"),
            )
        finally:
            SECRETS.remove("UNREQUESTED_VALUE", admin)
        self.assertTrue(result["ok"])
        self.assertIn("False", result["data"]["stdout"])

    def test_skill_team_and_required_environment_checks(self):
        denied_team = SANDBOXES.run("custos-team-contacts", "print('no')",
                                    UserContext("other", "other-team"))
        self.assertFalse(denied_team["ok"])

        admin = UserContext("admin", "platform", permissions=["admin"])
        meta = SkillMeta(
            skill_id="missing-env-test", name="Missing env test", description="test required env",
            version="1.0.0", secret_level="internal", system_prompt_snippet="test",
            output_schema={}, outline=[], required_env_vars=["NOT_CONFIGURED"],
            gray_teams=["custos-team"],
        )
        self.assertTrue(SKILLS.publish(meta, admin)["ok"])
        missing = SANDBOXES.run("missing-env-test", "print('should not start')",
                                UserContext("alice", "custos-team"))
        self.assertFalse(missing["ok"])
        self.assertIn("NOT_CONFIGURED", missing["msg"])
        self.assertFalse(any(row["action_type"] == "sandbox_run" for row in STORE.audit))

    def test_runtime_timeout_stops_child_process(self):
        admin = UserContext("admin", "platform", permissions=["admin"])
        meta = SkillMeta(
            skill_id="timeout-test-skill", name="Timeout test", description="test timeout",
            version="1.0.0", secret_level="internal", system_prompt_snippet="test",
            output_schema={}, outline=[], gray_teams=["custos-team"],
        )
        self.assertTrue(SKILLS.publish(meta, admin)["ok"])
        old_config = STORE.runtime_config
        STORE.runtime_config = RuntimeConfig(max_runtime_seconds=1, cpu_cores=16, memory_mb=512)
        try:
            result = SANDBOXES.run("timeout-test-skill", "while True: pass",
                                   UserContext("alice", "custos-team"))
        finally:
            STORE.runtime_config = old_config
        self.assertTrue(result["data"]["timed_out"])

    def test_runtime_config_has_resource_and_network_policy(self):
        admin = UserContext("admin", "platform", permissions=["admin"])
        old = STORE.runtime_config
        try:
            configured = RUNTIME_CONFIG.configure(
                RuntimeConfig(max_runtime_seconds=45, cpu_cores=2, memory_mb=1024,
                              network_mode="allowlist", network_hosts=["custos.example", "*.example.com"]),
                admin,
            )
            self.assertTrue(configured["ok"])
            self.assertEqual(configured["data"]["max_runtime_seconds"], 45)
            self.assertFalse(configured["data"]["network_policy_enforced"])
            self.assertFalse(RUNTIME_CONFIG.configure(RuntimeConfig(network_mode="invalid"), admin)["ok"])
        finally:
            STORE.runtime_config = old

    def test_environment_variable_values_are_visible_but_not_audited(self):
        value = "/private/path/not-for-response"
        result = SECRETS.configure(
            SecretEnvVar("EXAMPLE_PATH", value, ["custos-team"]),
            UserContext("admin", "platform", permissions=["admin"]),
        )
        self.assertTrue(result["ok"])
        visible = next(item for item in SECRETS.list_public() if item["name"] == "EXAMPLE_PATH")
        self.assertEqual(visible["value"], value)
        self.assertNotIn(value, json.dumps(result))
        self.assertNotIn(value, json.dumps(STORE.audit))

    def test_mcp_runtime_config_and_generic_runner(self):
        user = UserContext("alice", "custos-team")
        config = mcp_call("get_skill_config", {"skill_id": "custos-team-contacts"}, user)
        self.assertEqual(config["structuredContent"]["required_env_vars"], ["CUSTOS_CONTACTS_DIR"])
        result = mcp_call("run_code", {
            "skill_id": "custos-team-contacts",
            "code": "import os; print(os.path.isabs(os.environ['CUSTOS_CONTACTS_DIR']))",
        }, user)
        self.assertFalse(result.get("isError", False))
        self.assertIn("True", result["structuredContent"]["stdout"])

    def test_workbuddy_bundle_contains_skill_and_mcp_config(self):
        weekly = SKILLS.effective("weekly-report", UserContext("alice", "custos-team"))["data"]
        self.assertIn("Custos 团队周报", weekly["system_prompt_snippet"])
        bundle = workbuddy_bundle(weekly, "http://localhost:8000", UserContext("alice", "custos-team"))
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            skill = archive.read("skills/weekly-report/SKILL.md").decode()
            visible_skill = archive.read("SKILL.md").decode()
            config = json.loads(archive.read("mcp.json"))
        self.assertNotIn("Custos 团队周报", skill)
        self.assertNotIn("Custos 团队周报", visible_skill)
        self.assertEqual(skill, visible_skill)
        self.assertEqual(config["mcpServers"]["custos"]["url"], "http://localhost:8000/mcp")
        self.assertEqual(config["mcpServers"]["custos"]["type"], "streamableHttp")
        self.assertNotIn("X-Model-Bypass", json.dumps(config))

    def test_mock_skill_generation_handles_contacts(self):
        generated = GENERATOR.mock_generate("创建查看 Custos 团队联系人列表和读取邮箱的 Skill")
        self.assertEqual(generated["required_env_vars"], ["CUSTOS_CONTACTS_DIR"])
        self.assertEqual(generated["skill_id"], "custos-team-contacts")

    def test_mock_test_questions_use_skill_definition_and_guidance(self):
        skill = SKILLS.effective("weekly-report", UserContext("alice", "custos-team"))["data"]
        questions, mode = GENERATOR.generate_test_questions(skill, "重点测试缺失信息")
        self.assertEqual(mode, "mock")
        self.assertEqual(len(questions), 3)
        self.assertTrue(any("周报" in question for question in questions))
        self.assertTrue(any("缺失信息" in question for question in questions))

    def test_mcp_tool_calls_are_logged_without_recording_code(self):
        user = UserContext("alice", "custos-team")
        mcp_call("get_skill_config", {"skill_id": "custos-team-contacts"}, user)
        source = "print('do not store source')"
        result = mcp_call("run_code", {"skill_id": "custos-team-contacts", "code": source}, user)
        self.assertFalse(result.get("isError", False))
        tool_calls = [row for row in STORE.audit if row["action_type"] == "tool_call"]
        self.assertEqual([row["tool_name"] for row in tool_calls], ["get_skill_config", "run_code"])
        run_call = tool_calls[-1]
        self.assertNotIn(source, json.dumps(run_call))
        self.assertEqual(run_call["input_snapshot"]["code_bytes"], len(source.encode()))


if __name__ == "__main__":
    unittest.main()
