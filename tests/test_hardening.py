from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class HardeningPrerequisiteTests(unittest.TestCase):
    def test_auditd_is_installed_before_the_cis_role_runs(self):
        plays = yaml.safe_load((ROOT / "ansible/harden.yml").read_text())
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


if __name__ == "__main__":
    unittest.main()
