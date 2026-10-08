from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class HardeningPrerequisiteTests(unittest.TestCase):
    def test_auditd_is_installed_before_the_cis_role_runs(self):
        plays = yaml.safe_load((ROOT / "ansible/playbooks/harden.yml").read_text())
        play = plays[0]
        role_index = next(
            index
            for index, task in enumerate(play["tasks"])
            if task.get("ansible.builtin.import_role", {}).get("name") == "ubuntu24_cis"
        )
        self.assertEqual(role_index, 0)

        preinstalled_packages = {
            package
            for pre_task in play["pre_tasks"]
            for task in pre_task.get("block", [])
            for package in task.get("ansible.builtin.apt", {}).get("name", [])
        }
        self.assertTrue(
            {"auditd", "audispd-plugins"}.issubset(preinstalled_packages),
            "The pinned CIS role needs auditd in its initial package facts so "
            "discovered_auditd_immutable_check contains rc when handlers run.",
        )

    def test_aide_controls_are_disabled_and_packages_removed(self):
        cis_vars = yaml.safe_load((ROOT / "ansible/vars/cis.yml").read_text())
        self.assertFalse(cis_vars["ubtu24cis_rule_6_3_1"])
        self.assertFalse(cis_vars["ubtu24cis_rule_6_3_2"])
        self.assertFalse(cis_vars["ubtu24cis_rule_6_3_3"])

        plays = yaml.safe_load((ROOT / "ansible/playbooks/finalize.yml").read_text())
        removed = {
            package
            for task in plays[0]["tasks"]
            for package in task.get("ansible.builtin.apt", {}).get("name", [])
            if task.get("ansible.builtin.apt", {}).get("state") == "absent"
        }
        self.assertTrue({"aide", "aide-common"}.issubset(removed))

    def test_runtime_removes_build_only_curl(self):
        plays = yaml.safe_load((ROOT / "ansible/playbooks/runtime.yml").read_text())
        tasks = plays[0]["tasks"]
        removed = {
            package
            for task in tasks
            for package in task.get("ansible.builtin.apt", {}).get("name", [])
            if task.get("ansible.builtin.apt", {}).get("state") == "absent"
        }
        self.assertIn("curl", removed)
        bootstrap = (ROOT / "ansible/files/dragonwilds-bootstrap.sh").read_text()
        self.assertNotIn("attach-volume", bootstrap)
        self.assertNotIn("169.254.169.254", bootstrap)
        workload = (ROOT / "cloudformation/nested/workload.yaml").read_text()
        self.assertIn("Type: AWS::EC2::VolumeAttachment", workload)
        self.assertNotIn("ec2:AttachVolume", workload)

    def test_hardening_changes_trigger_a_full_build(self):
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertIn("playbooks/(bake|harden)\\.yml", workflow)
        self.assertNotIn("tasks/hardening\\.yml", workflow)

    def test_falco_enables_stable_and_endpoint_rules(self):
        config = yaml.safe_load((ROOT / "ansible/files/falco-dragonwilds.yaml").read_text())
        enabled_tags = {
            entry["enable"]["tag"]
            for entry in config["rules"]
            if "enable" in entry and "tag" in entry["enable"]
        }
        self.assertIn("maturity_stable", enabled_tags)

        objects = yaml.safe_load((ROOT / "ansible/files/falco-dragonwilds-rules.yaml").read_text())
        rules = {item["rule"] for item in objects if "rule" in item}
        self.assertGreaterEqual(len(rules), 15)
        self.assertTrue(
            {
                "Protected system path modified",
                "Account or group management tool executed",
                "Kernel module changed",
                "Sensitive credential file opened",
                "SSM interactive shell started",
            }.issubset(rules)
        )


if __name__ == "__main__":
    unittest.main()
