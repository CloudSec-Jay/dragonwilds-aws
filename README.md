# dragonwilds-aws

[![CI/CD Pipeline](https://github.com/CloudSec-Jay/dragonwilds-aws/actions/workflows/deploy.yml/badge.svg)](https://github.com/CloudSec-Jay/dragonwilds-aws/actions/workflows/deploy.yml)
[![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04_LTS-E95420?style=flat&logo=ubuntu&logoColor=white)](https://ubuntu.com)
[![CIS Benchmark](https://img.shields.io/badge/CIS%20Benchmark-88.40%25-success?style=flat&logo=checkmarx&logoColor=white)](https://www.cisecurity.org/)
[![Falco eBPF](https://img.shields.io/badge/Runtime%20Security-Falco%20eBPF-00AEC7?style=flat&logo=falco&logoColor=white)](https://falco.org/)
[![Trivy Scanned](https://img.shields.io/badge/Security-Trivy%20Scanned-1E88E5?style=flat&logo=aqua&logoColor=white)](https://trivy.dev/)
[![Unit Tests](https://img.shields.io/badge/Unit%20Tests-Passing-brightgreen?style=flat&logo=python&logoColor=white)](https://github.com/CloudSec-Jay/dragonwilds-aws/actions)
[![AWS CloudFormation](https://img.shields.io/badge/AWS-CloudFormation-FF9900?style=flat&logo=amazon-aws&logoColor=white)](https://aws.amazon.com/cloudformation/)
[![Packer](https://img.shields.io/badge/Packer-Automated%20AMI-02A8EF?style=flat&logo=packer&logoColor=white)](https://www.packer.io/)
[![Ansible](https://img.shields.io/badge/Ansible-CIS%20Automation-EE0000?style=flat&logo=ansible&logoColor=white)](https://www.ansible.com/)
[![Podman](https://img.shields.io/badge/Container-Podman%20Quadlet-892CA0?style=flat&logo=podman&logoColor=white)](https://podman.io/)

Infrastructure-as-code and runtime security for hosting a hardened [RuneScape: Dragonwilds](https://store.steampowered.com/app/1374490/RuneScape_Dragonwilds/) dedicated server on AWS EC2.

---

## Architecture Overview

A hardened Ubuntu 24.04 AMI is built with Packer and Ansible, then deployed via CloudFormation. The game server runs as a system Podman container managed by systemd via a Quadlet unit file, continuously monitored at the kernel level by Falco (eBPF) and File Integrity Monitoring (AIDE).

![Dragonwilds AWS reference architecture](docs/diagrams/dragonwilds_architecture_v5.svg)

```
Packer
  ├── image.yml               # Installs Podman, pre-pulls container image, configures Falco, purges bloat
  ├── harden.yml              # Applies CIS Level 1 hardening (ansible-lockdown role)
  │     └── vars/cis.yml      # Benchmark overrides for containerized EC2 host
  └── finalize.yml            # Runtime exclusions and final AIDE baseline

CloudFormation
  └── server.yaml             # VPC, EC2, UFW, EBS world volume, IAM roles, CloudWatch Logs

Runtime Stack
  ├── Telemetry & Monitoring
  │     ├── Falco (modern eBPF kernel tracing -> /var/log/falco_alerts.json)
  │     ├── CloudWatch Agent (streams falco-security & cloud-init to CloudWatch Logs)
  │     └── AIDE (cryptographic baseline file integrity monitoring)
  │
  └── Container Execution
        ├── dragonwilds-bootstrap.service (EBS discovery, mount, secret retrieval)
        └── dragonwilds.container (Quadlet systemd service)
              └── Image: ghcr.io/runescape/rsdw-dedicated (pre-baked in AMI)
              └── Volume: /srv/dragonwilds (retained encrypted EBS)
              └── Ports: 7777/udp (game), 8888/udp (beacon)
```

---

## Repository Layout

```
.
├── ansible/
│   ├── image.yml                      # Image preparation, container pre-pull, Falco setup, package cleanup
│   ├── harden.yml                     # CIS Level 1 hardening playbook
│   ├── finalize.yml                   # Final AIDE baseline, after hardening
│   ├── requirements.yml               # Pinned ansible-lockdown dependencies
│   ├── vars/
│   │   └── cis.yml                    # CIS benchmark overrides
│   └── roles/
│       └── ubuntu24_cis/              # Pinned UBUNTU24-CIS role
├── cloudformation/
│   └── server.yaml                    # VPC, EC2, IAM, EBS, UFW routing, CloudWatch Agent
├── ansible/files/
│   ├── dragonwilds.container          # Podman Quadlet unit file (zero-dependency health check)
│   ├── dragonwilds-bootstrap.service  # Reboot-safe volume mount & secret injection
│   ├── dragonwilds-bootstrap.sh       # NVMe EBS dynamic attachment & secret retrieval
│   ├── falco-dragonwilds.yaml         # Falco JSON file output & rule whitelist
│   ├── falco-dragonwilds-rules.yaml    # Custom Falco security rules
│   └── falco-alerts.logrotate          # Daily alert rotation
├── scripts/
│   ├── detect_changes.py              # Select build mode across the entire push
│   └── deploy.py                      # AMI selection, staged disk handoff, recovery
├── tests/                             # Deployment and secret-format regression tests
└── packer/
    ├── dragonwilds.pkr.hcl            # Full production AMI build (CIS + runtime, ~25m)
    ├── dragonwilds-update.pkr.hcl     # Fast incremental build on base AMI (includes final AIDE baseline)
    └── cis-audit.pkr.hcl              # Non-mutating CIS audit scanner
```

---

## Security Architecture

### 1. Runtime Kernel Detection (Falco eBPF)
Falco runs via the `modern_ebpf` driver (`falco-modern-bpf.service`) without requiring kernel headers or compiler toolchains. The configuration enables **5 custom security rules**, plus four upstream detections: terminal shells in containers, log clearing, network redirection of standard streams, and dropped executables in containers. The custom rules are:

| Rule | Target Trigger | Mitigation / Tuning |
| :--- | :--- | :--- |
| **Dragonwilds data directory modified** | Detects writes, unlinks, or renames in `/srv/dragonwilds` or `/home/steam/rsdw-dedicated` | Excludes `RSDragonwildsSe` writes only for UID 1000 in the `rsdw-dedicated` container to eliminate alert noise while alerting on any external tampering. |
| **Sudo execution** | Execution of `sudo` anywhere on the host or inside containers | Alerts whenever privilege escalation occurs. |
| **Authentication attempt failed** | Successful write-opens of `/var/log/btmp` | Detects failed SSH, console, or PAM login attempts. |
| **Use of chattr** | Execution of `chattr` or `lsattr` | Detects attempts to set immutable flags (`+i`) for defense evasion. |
| **Use of Python** | Execution of a process whose name starts with `python` | Detects interactive scripts or living-off-the-land execution. |

- **Alert Destination**: Structured JSON written directly to `/var/log/falco_alerts.json`.
- **Build validation**: Falco runs with `--dry-run -o engine.kind=nodriver` during baking; configuration or rule errors fail the build.
- **Log Rotation**: Governed by `/etc/logrotate.d/falco-alerts` (daily rotation, 7-day local retention, compressed).

### 2. Log Telemetry (AWS CloudWatch Logs)
The CloudWatch Agent collects logs and flushes batches every 60 seconds into Log Group **`/${ProjectName}/${Environment}/system`**:
- **`{instance_id}/falco-security`**: Streams from `/var/log/falco_alerts.json`.
- **`{instance_id}/cloud-init`**: Streams from `/var/log/cloud-init-output.log`.
- **Host Metrics**: Namespace `CWAgent` tracks CPU, memory, and disk utilization on `/` and `/srv/dragonwilds`.
- **Automatic host recovery**: A `StatusCheckFailed_System` alarm invokes EC2 recovery after two consecutive failed one-minute checks, preserving the instance identity, Elastic IP, and attached EBS world volume while also notifying the SNS topic.

### 3. File Integrity Monitoring (AIDE)
- Container/world-data exclusions are installed before the CIS role's first AIDE scan. Initial and final scans have a 30-minute limit and report progress every 15 seconds.
- A baseline cryptographic database (`/var/lib/aide/aide.db`) is regenerated by `finalize.yml` after all image provisioning, including CIS hardening. Both full and incremental builds run this final step; a base image without AIDE fails explicitly.
- Periodic scans verify filesystem binaries, libraries, and configuration files against the cryptographic baseline.

### 4. Network & Host Hardening
- **Zero Inbound SSH**: TCP port 22 is disabled. Management access is strictly via **AWS Systems Manager (SSM) Session Manager**.
- **Strict UFW + Routing**: UFW defaults to deny incoming. Ports `7777/udp` and `8888/udp` are allowed inbound, and `ufw route allow` rules are explicitly enabled to allow Podman bridged NAT forwarding without dropped packets.
- **IMDSv2 Enforced**: Metadata hop limit set to 1, preventing container processes from accessing the EC2 instance role credentials.
- **Package Minimization**: Unnecessary bloat and potential living-off-the-land tools are purged during image baking:
  `git`, `ssh-import-id`, `ubuntu-drivers-common`, `usbutils`, `bpftrace`, `snapd`, `open-iscsi`, `needrestart`, `landscape-common`, `apport`, `unattended-upgrades`.

### 5. CIS Benchmark Compliance (88.40%)
The base operating system is hardened against the **CIS Ubuntu 24.04 LTS Benchmark (v1.0.0, Level 1 Server)** using the official `ansible-lockdown/UBUNTU24-CIS` automation with customized container overrides:

| Assessment Stage | Total Controls | Failed | Passed | Compliance % |
| :--- | :---: | :---: | :---: | :---: |
| **Pre-Remediation** (Stock Ubuntu 24.04 AMI) | 586 | 189 | 397 | **67.75%** |
| **Post-Remediation** (Hardened Golden Image) | 586 | 68* | **518** | **88.40%** |

*\*The 68 remaining non-compliant items are documented, intentional exemptions required to support the dedicated server runtime:*
- **Podman Container Support**: Preserves IP forwarding (`net.ipv4.ip_forward=1`) and bridge network namespaces.
- **AWS SSM Session Manager**: Retains SSM agent communication channels and daemon execution without requiring inbound SSH (Port 22).
- **CloudWatch Agent & Falco Telemetry**: Allows system metric streaming and modern eBPF kernel tracing.
- **Clock Skew Tolerances**: Disables non-deterministic password expiration checks during short-lived Packer image builds.

---

## Container & World Storage

### Quadlet Service (`dragonwilds.container`)
- Managed natively by systemd (`systemd/generator`).
- **Pre-Baked Image**: `ghcr.io/runescape/rsdw-dedicated` is pre-pulled directly into the AMI during image baking, eliminating multi-minute image downloads on instance boot.
- **Zero-Dependency Health Check**: Uses direct procfs UDP table inspection (`grep -qi ':1E61 ' /proc/net/udp || grep -qi ':1E61 ' /proc/net/udp6`), removing dependencies on `ss` or `netstat`.
- Restarts automatically (`Restart=always`, `RestartSec=15`).
- Initial SteamCMD download window supported via `TimeoutStartSec=900`.

### Dynamic Storage & Bootstrap (`dragonwilds-bootstrap.sh`)
- World data is stored on a separate retained gp3 EBS volume mounted at `/srv/dragonwilds`.
- Discovers the EBS volume dynamically by serial number across `/dev/disk/by-id/` and `lsblk` NVMe identifiers.
- Dynamically discovers the filesystem UUID and adds an `/etc/fstab` entry on first bootstrap. If replacing the filesystem on an existing instance with a different UUID, update that entry before rebooting.
- Fetches all ten official `RSDW_*` container settings from AWS Secrets Manager and writes them to ephemeral memory at `/run/dragonwilds/server.env`.
- Can import an existing `.sav` from S3 before the container starts. The selected S3 object is imported once; later reboots keep the live EBS copy. If an old or automatically generated save already exists, it is moved to `RSDragonwilds/Saved/S3ImportBackups/` first.

---

## Building & Deploying

### Prerequisites
```bash
# 1. Install pinned Ansible roles
ansible-galaxy install -r ansible/requirements.yml

# 2. Authenticate locally with the AWS CLI before building or deploying.
# Keep AWS configuration and credentials outside this repository.
```

Local commands use the AWS SDK credential chain. Choose your AWS profile in your local shell or CLI configuration; CI uses GitHub OIDC and the server uses its EC2 IAM role. Do not commit profile files or exported credentials.

### Option A: Incremental AMI Layering
If you already have a hardened base AMI, bake runtime, container pre-pull, and Falco updates without reapplying the CIS role:
```bash
pip install boto3 PyYAML
export AWS_DEFAULT_REGION=us-east-1
BASE_AMI=$(python scripts/deploy.py resolve-ami)
packer init packer/dragonwilds-update.pkr.hcl
packer build -var "base_ami_id=$BASE_AMI" packer/dragonwilds-update.pkr.hcl
```

### Option B: Full AMI Build (~25 minutes)
Builds the base OS, applies the full CIS Level 1 benchmark, and layers the runtime stack:
```bash
packer build packer/dragonwilds.pkr.hcl
```

### Deploying the CloudFormation Stack

Use the deployment script for updates so the world disk is detached before an instance replacement:

```bash
pip install boto3 PyYAML
export AWS_DEFAULT_REGION=us-east-1
# Initial deployment only: set SERVER_SECRET_ARN to the existing Secrets Manager ARN.
# Secret values stay in AWS Secrets Manager; do not put them in this repository.
python scripts/deploy.py deploy --ami <NEW_AMI_ID>
```

For infrastructure-only updates, omit `--ami`; the existing stack's AMI and secret ARN are preserved. `BASE_AMI_ID` is only a fallback before the first stack exists. CI fast builds also resolve their base from the deployed stack, so successful master builds become the base automatically. All ten secret fields must be JSON strings, and line breaks or NUL bytes cannot be represented in Podman's environment-file format.

### Configure ownership and server management

The Secrets Manager value must be a JSON object with these exact keys:

```json
{
  "RSDW_OWNER_ID": "your Player ID from the bottom of the in-game Settings menu",
  "RSDW_PORT": "7777",
  "RSDW_BEACON_PORT": "8888",
  "RSDW_SERVER_NAME": "My Dragonwilds Server",
  "RSDW_WORLD_NAME": "My World",
  "RSDW_PASSWORD": "",
  "RSDW_ADMINS": "the same Player ID used for RSDW_OWNER_ID",
  "RSDW_ADMIN_PASSWORD": "a strong management password",
  "RSDW_ADDITIONAL_ARGS": "",
  "RSDW_AUTO_STOP_ON_UPDATE": "true"
}
```

`RSDW_PASSWORD` may be empty for a public world, and `RSDW_ADDITIONAL_ARGS` may be explicitly empty. `RSDW_ADMINS` is required and is set to the same private Player ID as `RSDW_OWNER_ID`. The ports are fixed to `7777` and `8888` so they match the published container and firewall ports. `RSDW_AUTO_STOP_ON_UPDATE=true` lets systemd restart the container when the image detects a Steam update. `RSDW_ADMIN_PASSWORD` is the password used in **Pause Menu > Settings > Server Management**; `RSDW_OWNER_ID` grants owner permissions to that player. Update the existing secret before starting the new image. This example prompts without putting either password in shell history and streams the JSON directly to Secrets Manager:

```bash
read -r -p 'Player ID: ' RSDW_OWNER_ID
read -r -p 'Server name: ' RSDW_SERVER_NAME
read -r -p 'Default world name: ' RSDW_WORLD_NAME
read -r -s -p 'World password (blank for public): ' RSDW_PASSWORD; printf '\n'
read -r -s -p 'Admin password: ' RSDW_ADMIN_PASSWORD; printf '\n'
read -r -p 'Additional server arguments (blank for none): ' RSDW_ADDITIONAL_ARGS

jq -n \
  --arg owner "$RSDW_OWNER_ID" \
  --arg port "7777" \
  --arg beacon "8888" \
  --arg server "$RSDW_SERVER_NAME" \
  --arg world "$RSDW_WORLD_NAME" \
  --arg password "$RSDW_PASSWORD" \
  --arg admins "$RSDW_OWNER_ID" \
  --arg admin "$RSDW_ADMIN_PASSWORD" \
  --arg additional "$RSDW_ADDITIONAL_ARGS" \
  --arg autostop "true" \
  '{RSDW_OWNER_ID:$owner,RSDW_PORT:$port,RSDW_BEACON_PORT:$beacon,RSDW_SERVER_NAME:$server,RSDW_WORLD_NAME:$world,RSDW_PASSWORD:$password,RSDW_ADMINS:$admins,RSDW_ADMIN_PASSWORD:$admin,RSDW_ADDITIONAL_ARGS:$additional,RSDW_AUTO_STOP_ON_UPDATE:$autostop}' \
| aws secretsmanager put-secret-value \
    --secret-id "$SERVER_SECRET_ARN" \
    --secret-string file:///dev/stdin

unset RSDW_OWNER_ID RSDW_SERVER_NAME RSDW_WORLD_NAME RSDW_PASSWORD RSDW_ADMIN_PASSWORD RSDW_ADDITIONAL_ARGS
```

### Import an existing world from S3

Upload the local world `.sav` from `%LOCALAPPDATA%\RSDragonwilds\Saved\SaveGames` to an existing S3 bucket. Then deploy an AMI containing this bootstrap change and select the exact object:

```bash
python scripts/deploy.py deploy \
  --ami <NEW_AMI_ID> \
  --world-save-bucket <EXISTING_BUCKET_NAME> \
  --world-save-key <PATH/TO/WORLD.sav>
```

The instance role receives read access only to that object. Import happens before the game starts. To deliberately import a different world later, deploy with a different object key; the current save is archived on the retained EBS volume before replacement. Keep the S3 source as an independent backup.

#### Recover a world from Steam Cloud

1. Sign in to [Steam Remote Storage](https://store.steampowered.com/account/remotestorage), find RuneScape: Dragonwilds, and open its files.
2. Download the desired `.sav`, not the smaller `.sav.backup`. Steam may flatten the Windows path into a filename such as `%WinAppDataLocal%RSDragonwilds_Saved_SaveGames_homeworld.sav`.
3. Upload the file to a private S3 bucket. SSE-S3 is sufficient; it does not require a customer-managed KMS key.
4. Record the bucket name and object key separately. For `s3://example-bucket/saves/homeworld.sav`, the bucket is `example-bucket` and the key is `saves/homeworld.sav`. In an HTTPS URL, `%25` represents a literal `%` in the object key.
5. Store these identifiers as GitHub Actions secrets and start a deploy-only run. They are not credentials, but secrets keep private bucket and save names out of public workflow configuration:

   ```bash
   printf '%s' '<BUCKET_NAME>' | gh secret set WORLD_SAVE_BUCKET
   printf '%s' '<OBJECT_KEY>' | gh secret set WORLD_SAVE_KEY
   gh workflow run deploy.yml --ref main -f build_type=deploy-only
   ```

6. After deployment, connect with SSM and verify the one-time import marker:

   ```bash
   sudo cat /srv/dragonwilds/RSDragonwilds/Saved/.s3-world-source
   ```

An in-place EC2 update changes the CloudFormation parameters and IAM permission but does not necessarily rerun cloud-init. If the marker is absent, keep the game stopped, populate `WORLD_SAVE_BUCKET` and `WORLD_SAVE_KEY` in `/etc/dragonwilds/bootstrap.env`, and restart `dragonwilds-bootstrap.service`. Do not run the importer while the game is writing its save.

Steam's flattened filename is retained when S3 is imported. Before starting the game, rename it to match `RSDW_WORLD_NAME` exactly, including capitalization, and preserve the `.sav` suffix. The importer sets UID/GID `1000:1000`, archives existing saves under `RSDragonwilds/Saved/S3ImportBackups/`, and records the S3 source so later reboots do not overwrite the live world.

#### Recovery challenges and checks

- Every Secrets Manager `RSDW_*` value must be a JSON string. Ports use `"7777"` and `"8888"`, and the Boolean-like value is `"true"`; missing keys evaluate as `null` and stop bootstrap.
- `WORLD_SAVE_KEY` means the S3 object path, not an encryption key. SSE-S3 needs no KMS key.
- A successful CloudFormation update proves the infrastructure update completed, not that cloud-init reran or the save was imported. Verify the marker, bootstrap service, container, and files explicitly.
- Do not execute a manual change set when `DragonwildsInstance` or `DragonwildsDataVolumeAttachment` shows conditional replacement. Use `scripts/deploy.py` or the deploy-only workflow so the instance is stopped and the retained EBS volume is detached safely.
- `dragonwilds-bootstrap.service` must finish as `active (exited)` before `dragonwilds.service` can start. Use `journalctl -u dragonwilds-bootstrap.service` to identify malformed or missing secret fields.
- The selected filename must match `RSDW_WORLD_NAME` on Linux, where capitalization matters. Check `/run/dragonwilds/server.env` and `RSDragonwilds/Saved/SaveGames/` before starting the container.

A replacement has planned downtime. The script previews a CloudFormation change set, rejects world-volume replacement, shuts down the old EC2 instance normally, and removes only the attachment from the currently deployed template. It then applies the new template, which attaches the retained disk to the replacement. Do not run a direct CloudFormation AMI update against an attached world disk.

If an update fails and CloudFormation finishes rolling back, the script restores the old template and parameters, reattaches the disk, and restarts the previous server if it was running. A failed rollback or interrupted runner requires recovery after CloudFormation returns to a stable state. CI preserves `deployment-recovery.json` as a seven-day artifact when available; it contains stack configuration and the secret ARN, not secret values. With the matching AWS account and region selected:

```bash
python scripts/deploy.py recover --recovery-file deployment-recovery.json
```

The deployment role needs CloudFormation change-set/read/update permissions, `ec2:DescribeInstances`, `ec2:StopInstances`, and `ec2:StartInstances`, in addition to its existing infrastructure and Packer permissions. The stop operation never uses force. Only deploy from one controller at a time; GitHub Actions serializes deployments.

Automatic push detection compares the push's `before` and final SHA. Manual `auto`, scheduled builds, and an unavailable diff base select a full master build.

### Local checks

```bash
pip install boto3 PyYAML jq
python -m unittest discover -s tests -v
```

The tests simulate AWS operations and verify stop/detach/deploy ordering, recovery, AMI selection, multi-commit pushes, and exact secret formatting. They do not deploy resources. A real AMI bake and replacement are still needed to verify the complete EC2 boot path.

---

## Verification & Operational Runbook

### Inspect Runtime Security Alerts
On the live EC2 host:
```bash
# Tail local Falco alerts
sudo tail -n 10 /var/log/falco_alerts.json

# Check CloudWatch Agent shipping status
sudo tail -n 20 /opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log
```

From AWS CLI / Workstation:
```bash
# Query recent Falco alerts from CloudWatch Logs
aws logs get-log-events \
  --log-group-name /dragonwilds/production/system \
  --log-stream-name "$(aws ec2 describe-instances --filters "Name=tag:Name,Values=dragonwilds-production-server" "Name=instance-state-name,Values=running" --query "Reservations[0].Instances[0].InstanceId" --output text)/falco-security" \
  --limit 5
```

### Run File Integrity Check (AIDE)
```bash
sudo aide --config /etc/aide/aide.conf --check
```

### Inspect CIS Benchmark Compliance Score
To inspect the audit score from generated Goss benchmark reports:
```bash
python3 -c "
import json, glob
report = sorted(glob.glob('ansible/audit-reports/*post_scan*.json'))[-1]
with open(report) as f:
    s = json.load(f)['summary']
passed = s['test-count'] - s['failed-count']
pct = (passed / s['test-count']) * 100
print(f'CIS Benchmark Score: {pct:.2f}% ({passed}/{s[\"test-count\"]} checks passed, {s[\"failed-count\"]} exempt)')
"
```
