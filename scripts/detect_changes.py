"""Select a bake mode using the entire GitHub push, not only its last commit."""

import os
import subprocess


def select_build(event, requested="auto", before="", after="HEAD"):
    if event == "schedule" or requested == "master":
        return "master"
    if requested in ("fast", "deploy-only"):
        return requested.replace("-", "_")
    # A manual auto run reconciles everything; an unknown push base must not skip.
    if event != "push" or not before or set(before) == {"0"}:
        return "master"
    try:
        files = subprocess.check_output(
            ["git", "diff", "--name-only", before, after, "--"], text=True
        ).splitlines()
    except subprocess.CalledProcessError:
        return "master"
    master = {
        "ansible/harden.yml", "ansible/vars/cis.yml", "ansible/requirements.yml",
        "packer/dragonwilds.pkr.hcl",
    }
    if master.intersection(files):
        return "master"
    if any(f.startswith(("ansible/", "packer/")) for f in files):
        return "fast"
    if any(f.startswith("cloudformation/") for f in files):
        return "deploy_only"
    return "skip"


if __name__ == "__main__":
    result = select_build(
        os.environ["GITHUB_EVENT_NAME"], os.getenv("REQUESTED_BUILD", "auto"),
        os.getenv("PUSH_BEFORE", ""), os.getenv("GITHUB_SHA", "HEAD"),
    )
    print(f"Selected build type: {result}")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"build_type={result}\n")
