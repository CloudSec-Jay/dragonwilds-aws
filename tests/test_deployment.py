import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, WaiterError

from scripts import deploy
from scripts.detect_changes import select_build

ROOT = Path(__file__).resolve().parents[1]
OLD_AMI = "ami-11111111111111111"
NEW_AMI = "ami-22222222222222222"
BASE_AMI = "ami-33333333333333333"


class ChangeDetectionTests(unittest.TestCase):
    def test_whole_push_includes_hardening_before_documentation(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(["git", "-C", directory, *args], text=True).strip()

            git("init", "-q")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Regression test")
            git("commit", "--allow-empty", "-qm", "initial")
            before = git("rev-parse", "HEAD")
            path = Path(directory) / "ansible"
            path.mkdir()
            (path / "harden.yml").write_text("---\n")
            git("add", ".")
            git("commit", "-qm", "hardening")
            (Path(directory) / "README.md").write_text("docs\n")
            git("add", ".")
            git("commit", "-qm", "docs")
            previous = os.getcwd()
            try:
                os.chdir(directory)
                self.assertEqual(select_build("push", before=before), "master")
            finally:
                os.chdir(previous)

    def test_triggers_and_unknown_base(self):
        self.assertEqual(select_build("schedule"), "master")
        self.assertEqual(select_build("workflow_dispatch"), "master")
        self.assertEqual(select_build("workflow_dispatch", "deploy-only"), "deploy_only")
        self.assertEqual(select_build("push", before="0" * 40), "master")

    def test_priorities(self):
        for paths, expected in [
            ("README.md\n", "skip"),
            ("cloudformation/server.yaml\n", "deploy_only"),
            ("cloudformation/server.yaml\nansible/files/file\n", "fast"),
            ("ansible/image.yml\nansible/vars/cis.yml\n", "master"),
        ]:
            with self.subTest(paths=paths), patch(
                "scripts.detect_changes.subprocess.check_output", return_value=paths
            ):
                self.assertEqual(select_build("push", before="abc123"), expected)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.template = deploy.read_template((ROOT / "cloudformation/server.yaml").read_text())
        self.stack = {
            "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test/1",
            "StackStatus": "UPDATE_COMPLETE",
            "Parameters": [
                {"ParameterKey": "AmiId", "ParameterValue": OLD_AMI},
                {"ParameterKey": "ServerSecretArn", "ParameterValue": "arn:aws:secretsmanager:example"},
                {"ParameterKey": "DataVolumeSize", "ParameterValue": "42"},
            ],
        }
        self.cfn, self.ec2 = Mock(), Mock()
        self.cfn.describe_stacks.return_value = {"Stacks": [self.stack]}
        self.cfn.get_template.return_value = {"TemplateBody": self.template}
        self.cfn.describe_stack_resource.return_value = {
            "StackResourceDetail": {"PhysicalResourceId": "i-old"}
        }
        self.ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
        }
        self.replacement = [{
            "LogicalResourceId": deploy.INSTANCE, "Action": "Modify", "Replacement": "True"
        }]

    def test_deploy_only_and_fast_use_deployed_image(self):
        self.assertEqual(deploy.select_ami(self.stack, initial=BASE_AMI), OLD_AMI)
        self.assertEqual(deploy.select_ami(self.stack, NEW_AMI, BASE_AMI), NEW_AMI)
        self.assertEqual(deploy.select_ami(None, initial=BASE_AMI), BASE_AMI)
        with self.assertRaises(ValueError):
            deploy.select_ami(None)

    def test_deploy_only_does_not_stop_instance(self):
        with patch.object(deploy, "plan", return_value=("plan-id", [])) as plan, \
                patch.object(deploy, "wait_stack"):
            deploy.deploy(self.cfn, self.ec2, "test", self.template, initial=BASE_AMI)
        self.assertEqual(plan.call_args.args[3]["AmiId"], OLD_AMI)
        self.assertEqual(plan.call_args.args[3]["DataVolumeSize"], "42")
        self.ec2.stop_instances.assert_not_called()

    def test_world_save_parameters_are_applied(self):
        with patch.object(deploy, "plan", return_value=("plan-id", [])) as plan, \
                patch.object(deploy, "wait_stack"):
            deploy.deploy(
                self.cfn, self.ec2, "test", self.template,
                overrides={"WorldSaveBucket": "worlds-example", "WorldSaveKey": "saves/home.sav"},
            )
        values = plan.call_args.args[3]
        self.assertEqual(values["WorldSaveBucket"], "worlds-example")
        self.assertEqual(values["WorldSaveKey"], "saves/home.sav")

    def test_world_save_key_accepts_percent_encoded_filename(self):
        pattern = self.template["Parameters"]["WorldSaveKey"]["AllowedPattern"]
        self.assertRegex(
            "%WinAppDataLocal%RSDragonwilds_Saved_SaveGames_homeworld.sav",
            re.compile(pattern),
        )

    def test_system_failure_alarm_recovers_the_instance(self):
        alarm = self.template["Resources"]["DragonwildsSystemRecoveryAlarm"]["Properties"]
        self.assertEqual(alarm["MetricName"], "StatusCheckFailed_System")
        self.assertEqual(alarm["TreatMissingData"], "notBreaching")
        self.assertIn(
            {"Fn::Sub": "arn:${AWS::Partition}:automate:${AWS::Region}:ec2:recover"},
            alarm["AlarmActions"],
        )

    def test_initial_stack_does_not_try_to_detach(self):
        with patch.object(deploy, "get_stack", return_value=None), \
                patch.object(deploy, "plan", return_value=("plan-id", [])) as plan, \
                patch.object(deploy, "wait_stack") as wait:
            deploy.deploy(self.cfn, self.ec2, "test", self.template, NEW_AMI,
                          secret="arn:aws:secretsmanager:example")
        self.assertTrue(plan.call_args.kwargs["creating"])
        wait.assert_called_once_with(self.cfn, "test", creating=True)
        self.ec2.stop_instances.assert_not_called()

    def test_no_changes_does_not_execute_or_stop(self):
        with patch.object(deploy, "plan", return_value=(None, [])):
            deploy.deploy(self.cfn, self.ec2, "test", self.template)
        self.cfn.execute_change_set.assert_not_called()
        self.ec2.stop_instances.assert_not_called()

    def test_access_denied_is_not_treated_as_missing_stack(self):
        self.cfn.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeStacks"
        )
        with self.assertRaises(ClientError):
            deploy.get_stack(self.cfn, "test")

    def test_plan_checks_all_change_set_pages(self):
        self.cfn.create_change_set.return_value = {"Id": "plan-id"}
        self.cfn.describe_change_set.side_effect = [
            {"Changes": [], "NextToken": "page2"},
            {"Changes": [{"ResourceChange": self.replacement[0]}]},
        ]
        change_id, changes = deploy.plan(self.cfn, "test", self.template, deploy.parameters(self.stack))
        self.assertEqual(change_id, "plan-id")
        self.assertTrue(deploy.needs_detach(changes))

    def test_empty_change_set_is_success_and_is_deleted(self):
        self.cfn.create_change_set.return_value = {"Id": "plan-id"}
        self.cfn.get_waiter.return_value.wait.side_effect = WaiterError(
            name="change_set_create_complete", reason="failed", last_response={}
        )
        self.cfn.describe_change_set.return_value = {
            "StatusReason": "The submitted information didn't contain changes."
        }
        self.assertEqual(deploy.plan(self.cfn, "test", self.template, {}), (None, []))
        self.cfn.delete_change_set.assert_called_once_with(ChangeSetName="plan-id")

    def test_replacement_stops_then_detaches_only_then_deploys(self):
        events = []
        self.ec2.stop_instances.side_effect = lambda **kw: events.append("stop")
        self.ec2.get_waiter.return_value.wait.side_effect = lambda **kw: events.append("stopped")

        def apply(cfn, name, template, values):
            if deploy.ATTACHMENT not in template["Resources"]:
                events.append("detach")
                expected = copy.deepcopy(self.template)
                del expected["Resources"][deploy.ATTACHMENT]
                self.assertEqual(template, expected)
                self.assertEqual(values["AmiId"], OLD_AMI)
            else:
                events.append("deploy")
                self.assertEqual(values["AmiId"], NEW_AMI)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(deploy, "plan", return_value=("plan-id", self.replacement)), \
                patch.object(deploy, "apply", side_effect=apply):
            recovery = str(Path(directory) / "recovery.json")
            deploy.deploy(self.cfn, self.ec2, "test", self.template, NEW_AMI, recovery_file=recovery)
            self.assertFalse(Path(recovery).exists())
        self.assertEqual(events, ["stop", "stopped", "detach", "deploy"])

    def test_failed_update_restores_original_template_before_restart(self):
        calls = []
        self.ec2.describe_instances.side_effect = [
            {"Reservations": [{"Instances": [{"State": {"Name": state}}]}]}
            for state in ("running", "stopped")
        ]

        def apply(cfn, name, template, values):
            calls.append((template, values.copy()))
            if len(calls) == 2:
                raise RuntimeError("simulated rolled-back update")

        def restart(**kwargs):
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1][0], self.template)
            self.assertEqual(calls[-1][1]["AmiId"], OLD_AMI)

        self.ec2.start_instances.side_effect = restart
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(deploy, "plan", return_value=("plan-id", self.replacement)), \
                patch.object(deploy, "apply", side_effect=apply):
            recovery = str(Path(directory) / "recovery.json")
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                deploy.deploy(self.cfn, self.ec2, "test", self.template, NEW_AMI, recovery_file=recovery)
            self.assertEqual(json.loads(Path(recovery).read_text())["parameters"]["AmiId"], OLD_AMI)
        self.ec2.start_instances.assert_called_once_with(InstanceIds=["i-old"])

    def test_world_volume_replacement_is_rejected_before_stopping(self):
        change = [{"LogicalResourceId": deploy.VOLUME, "Action": "Modify", "Replacement": "Conditional"}]
        with patch.object(deploy, "plan", return_value=("plan-id", change)):
            with self.assertRaisesRegex(ValueError, "data migration"):
                deploy.deploy(self.cfn, self.ec2, "test", self.template, NEW_AMI)
        self.ec2.stop_instances.assert_not_called()
        self.cfn.execute_change_set.assert_not_called()

    def test_failed_rollback_does_not_restart_without_storage(self):
        self.stack["StackStatus"] = "UPDATE_ROLLBACK_FAILED"
        with self.assertRaisesRegex(RuntimeError, "Cannot recover"):
            deploy.recover(self.cfn, self.ec2, "test", self.template, {}, True)
        self.ec2.start_instances.assert_not_called()


if __name__ == "__main__":
    unittest.main()
