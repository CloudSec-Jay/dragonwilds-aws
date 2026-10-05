"""Deploy a single-instance server while preserving its single-attach world disk.

AMI replacements have downtime: shut down cleanly, remove only the attachment
from the deployed template, then deploy the requested template. Failed updates
restore the original template/parameters before restarting the original server.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError, WaiterError
import yaml

INSTANCE = "DragonwildsInstance"
VOLUME = "DragonwildsDataVolume"
ATTACHMENT = "DragonwildsDataVolumeAttachment"
READY = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}


class TemplateLoader(yaml.SafeLoader):
    """Read CloudFormation short-form tags without executing YAML constructors."""


def intrinsic(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    if tag == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {tag if tag in ("Ref", "Condition") else "Fn::" + tag: value}


TemplateLoader.add_multi_constructor("!", intrinsic)


def read_template(body):
    return yaml.load(body, Loader=TemplateLoader) if isinstance(body, str) else body


def get_stack(cfn, name):
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as exc:
        error = exc.response["Error"]
        if error["Code"] == "ValidationError" and "does not exist" in error["Message"]:
            return None
        raise


def parameters(stack):
    return {p["ParameterKey"]: p["ParameterValue"] for p in stack.get("Parameters", [])}


def select_ami(stack, baked="", initial=""):
    # BASE_AMI_ID is only a bootstrap fallback, never an implicit rollback.
    ami = baked or (parameters(stack).get("AmiId", "") if stack else "") or initial
    if not re.fullmatch(r"ami-(?:[0-9a-f]{8}|[0-9a-f]{17})", ami):
        raise ValueError("Supply a valid AMI for the initial deployment or build a master image")
    return ami


def stack_parameters(template, values):
    return [
        {"ParameterKey": key, "ParameterValue": values[key]}
        for key in template.get("Parameters", {}) if key in values
    ]


def plan(cfn, name, template, values, creating=False):
    response = cfn.create_change_set(
        StackName=name, ChangeSetName="dragonwilds-" + uuid.uuid4().hex,
        ChangeSetType="CREATE" if creating else "UPDATE",
        TemplateBody=json.dumps(template), Parameters=stack_parameters(template, values),
        Capabilities=["CAPABILITY_IAM"],
    )
    change_id = response["Id"]
    try:
        cfn.get_waiter("change_set_create_complete").wait(
            ChangeSetName=change_id, WaiterConfig={"Delay": 5, "MaxAttempts": 120}
        )
    except WaiterError:
        failed = cfn.describe_change_set(ChangeSetName=change_id)
        reason = failed.get("StatusReason", "")
        if "didn't contain changes" in reason or "No updates are to be performed" in reason:
            cfn.delete_change_set(ChangeSetName=change_id)
            return None, []
        raise RuntimeError(f"Could not prepare deployment: {reason}") from None
    changes = []
    request = {"ChangeSetName": change_id}
    while True:
        page = cfn.describe_change_set(**request)
        changes.extend(c["ResourceChange"] for c in page.get("Changes", []))
        if "NextToken" not in page:
            break
        request["NextToken"] = page["NextToken"]
    return change_id, changes


def wait_stack(cfn, name, creating=False):
    expected = "CREATE_COMPLETE" if creating else "UPDATE_COMPLETE"
    for _ in range(240):
        status = get_stack(cfn, name)["StackStatus"]
        if status == expected:
            return
        if not status.endswith("_IN_PROGRESS"):
            raise RuntimeError(f"Deployment ended with {status}; inspect CloudFormation events")
        time.sleep(10)
    raise TimeoutError("Stack operation exceeded 40 minutes; inspect it before retrying")


def apply(cfn, name, template, values, creating=False):
    change_id, _ = plan(cfn, name, template, values, creating)
    if change_id:
        cfn.execute_change_set(ChangeSetName=change_id)
        wait_stack(cfn, name, creating)


def needs_detach(changes):
    for change in changes:
        if change["LogicalResourceId"] == VOLUME and (
            change["Action"] == "Remove" or change.get("Replacement") in ("True", "Conditional")
        ):
            raise ValueError("World volume replacement requires a separate data migration")
    return any(
        c["LogicalResourceId"] in (INSTANCE, ATTACHMENT)
        and (c["Action"] == "Remove" or c.get("Replacement") in ("True", "Conditional"))
        for c in changes
    )


def physical_id(cfn, name, logical_id):
    return cfn.describe_stack_resource(StackName=name, LogicalResourceId=logical_id)[
        "StackResourceDetail"
    ]["PhysicalResourceId"]


def recover(cfn, ec2, name, original, values, was_running):
    status = get_stack(cfn, name)["StackStatus"]
    if status not in READY:
        raise RuntimeError(f"Cannot recover automatically while stack is {status}")
    apply(cfn, name, original, values)
    if was_running:
        instance = physical_id(cfn, name, INSTANCE)
        state = ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
        if state == "stopping":
            ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance])
            state = "stopped"
        if state == "stopped":
            ec2.start_instances(InstanceIds=[instance])
            ec2.get_waiter("instance_running").wait(InstanceIds=[instance])
        elif state != "running":
            raise RuntimeError(f"Recovered instance cannot be started from state {state}")


def deploy(cfn, ec2, name, template, ami="", initial="", secret="",
           recovery_file="deployment-recovery.json"):
    stack = get_stack(cfn, name)
    if stack and stack["StackStatus"] not in READY:
        raise ValueError(f"Stack is not ready for an update: {stack['StackStatus']}")
    values = parameters(stack) if stack else {}
    values["AmiId"] = select_ami(stack, ami, initial)
    if secret:
        values["ServerSecretArn"] = secret
    if not values.get("ServerSecretArn", "").startswith("arn:"):
        raise ValueError("SERVER_SECRET_ARN must be a Secrets Manager ARN")
    change_id, changes = plan(cfn, name, template, values, creating=stack is None)
    if change_id is None:
        print("No infrastructure changes")
        return
    try:
        staged = needs_detach(changes) if stack else False
    except ValueError:
        cfn.delete_change_set(ChangeSetName=change_id)
        raise
    if not staged:
        cfn.execute_change_set(ChangeSetName=change_id)
        wait_stack(cfn, name, creating=stack is None)
        return

    # The first plan is invalidated by staging; regenerate after disk detachment.
    cfn.delete_change_set(ChangeSetName=change_id)
    original = read_template(cfn.get_template(StackName=name)["TemplateBody"])
    if ATTACHMENT not in original["Resources"]:
        raise ValueError("Attachment is absent; recover the interrupted deployment first")
    detached = copy.deepcopy(original)
    del detached["Resources"][ATTACHMENT]
    instance = physical_id(cfn, name, INSTANCE)
    state = ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
    if state not in ("running", "stopped"):
        raise ValueError(f"Cannot prepare an instance in state {state}")
    original_values = parameters(stack)
    snapshot = {
        "stack": name, "stack_id": stack["StackId"],
        "template": original, "parameters": original_values,
        "was_running": state == "running",
    }
    with os.fdopen(os.open(recovery_file, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w") as output:
        output.write(json.dumps(snapshot, indent=2) + "\n")
    print("Stopping the server cleanly before detaching its world volume", flush=True)
    try:
        if state == "running":
            ec2.stop_instances(InstanceIds=[instance])  # Never force-stop or force-detach.
        ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance])
        apply(cfn, name, detached, original_values)
        apply(cfn, name, template, values)
    except Exception:
        print("Deployment failed; restoring the previous stack and attachment", flush=True)
        recover(cfn, ec2, name, original, original_values, state == "running")
        raise
    Path(recovery_file).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["resolve-ami", "deploy", "recover"])
    parser.add_argument("--stack-name", default="dragonwilds")
    parser.add_argument("--template", default="cloudformation/server.yaml")
    parser.add_argument("--ami", default=os.getenv("BAKED_AMI_ID", ""))
    parser.add_argument("--initial-ami", default=os.getenv("BASE_AMI_ID", ""))
    parser.add_argument("--recovery-file", default="deployment-recovery.json")
    args = parser.parse_args()
    cfn, ec2 = boto3.client("cloudformation"), boto3.client("ec2")
    if args.command == "resolve-ami":
        print(select_ami(get_stack(cfn, args.stack_name), initial=args.initial_ami))
    elif args.command == "recover":
        snapshot = json.loads(Path(args.recovery_file).read_text())
        current = get_stack(cfn, args.stack_name)
        if (snapshot["stack"] != args.stack_name or not current
                or current["StackId"] != snapshot["stack_id"]):
            raise ValueError("Recovery file belongs to another stack, account, or region")
        recover(cfn, ec2, args.stack_name, snapshot["template"],
                snapshot["parameters"], snapshot["was_running"])
    else:
        deploy(cfn, ec2, args.stack_name, read_template(Path(args.template).read_text()),
               args.ami, args.initial_ami, os.getenv("SERVER_SECRET_ARN", ""), args.recovery_file)


if __name__ == "__main__":
    main()
