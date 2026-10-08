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

A hardened Ubuntu 24.04 AMI is built with Packer and Ansible, then deployed via nested CloudFormation stacks. The game server runs as a system Podman container managed by systemd via a Quadlet unit file and is continuously monitored at the kernel level by Falco (eBPF). AIDE is deliberately disabled to avoid expensive scans of the immutable image and changing game data.

![Dragonwilds AWS reference architecture](docs/diagrams/dragonwilds_architecture_v13(2).svg)

```
Packer
  ├── playbooks/bake.yml      # Single Packer entry point
  ├── playbooks/runtime.yml   # Podman, Falco, agents, and runtime files
  ├── playbooks/harden.yml    # CIS Level 1 hardening (ansible-lockdown role)
  └── playbooks/finalize.yml  # Removes AIDE and cleans image artifacts

CloudFormation
  ├── server.yaml             # Parent stack
  └── nested/                 # Network and workload child stacks

Runtime Stack
  ├── Telemetry & Monitoring
  │     ├── Falco (modern eBPF kernel tracing -> /var/log/falco_alerts.json)
  │     ├── CloudWatch Agent (logs, host/process metrics, and local StatsD health)
  │     └── CloudWatch Dashboard (availability, utilization, network, disk, and Falco)
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
│   ├── playbooks/
│   │   ├── bake.yml                   # Unified full/fast image entry point
│   │   ├── runtime.yml                # Runtime and monitoring configuration
│   │   ├── harden.yml                 # CIS Level 1 hardening playbook
│   │   └── finalize.yml               # AIDE removal and package cleanup
│   ├── requirements.yml               # Pinned ansible-lockdown dependencies
│   ├── vars/
│   │   └── cis.yml                    # CIS benchmark overrides
│   └── roles/
│       └── ubuntu24_cis/              # Pinned UBUNTU24-CIS role
├── cloudformation/
│   ├── server.yaml                    # Parent stack and public parameters
│   └── nested/
│       ├── network.yaml               # VPC, subnet, routes, and security group
│       └── workload.yaml              # EC2, IAM, EBS, backup, logs, and alarms
├── ansible/files/
│   ├── dragonwilds.container          # Podman Quadlet unit file (zero-dependency health check)
│   ├── dragonwilds-bootstrap.service  # Reboot-safe volume mount & secret injection
│   ├── dragonwilds-bootstrap.sh       # Attached-EBS mount, import, and secret retrieval
│   ├── dragonwilds-health.sh          # Publishes service health to local StatsD
│   ├── dragonwilds-health.service     # One-shot health reporter
│   ├── dragonwilds-health.timer       # Runs health reporting every minute
│   ├── falco-dragonwilds.yaml         # Falco JSON file output & rule whitelist
│   ├── falco-dragonwilds-rules.yaml    # Custom Falco security rules
│   └── falco-alerts.logrotate          # Daily alert rotation
├── tests/                             # CIS prerequisite and secret-format regression tests
└── packer/
    ├── dragonwilds.pkr.hcl            # Full production AMI build (CIS + runtime, ~25m)
    ├── dragonwilds-update.pkr.hcl     # Fast incremental build; skips the tagged hardening play
    └── cis-audit.pkr.hcl              # Non-mutating CIS audit scanner
```

---

## Security Architecture

### 1. Runtime Kernel Detection (Falco eBPF)
Falco runs via the `modern_ebpf` driver (`falco-modern-bpf.service`) without requiring kernel headers or compiler toolchains. The configuration enables Falco's packaged `maturity_stable` ruleset plus **15 host-specific rules**. The upstream baseline covers sensitive-file access, destructive commands, log clearing, credential searches, suspicious container execution, reverse-shell behavior, container escape techniques, and other maintained runtime detections. The custom coverage is:

| Rule | Target Trigger | Mitigation / Tuning |
| :--- | :--- | :--- |
| **Dragonwilds data directory modified** | Detects writes, unlinks, or renames in `/srv/dragonwilds` or `/home/steam/rsdw-dedicated` | Excludes `RSDragonwildsSe` writes only for UID 1000 in the `rsdw-dedicated` container to eliminate alert noise while alerting on any external tampering. |
| **Sudo execution** | Execution of `sudo` anywhere on the host or inside containers | Alerts whenever privilege escalation occurs. |
| **Authentication attempt failed** | Successful write-opens of `/var/log/btmp` | Detects failed SSH, console, or PAM login attempts. |
| **Use of chattr** | Execution of `chattr` or `lsattr` | Detects attempts to set immutable flags (`+i`) for defense evasion. |
| **Use of Python** | Execution of a process whose name starts with `python` | Detects interactive scripts or living-off-the-land execution. |
| **Protected system path modified** | Changes to identity, PAM, SSH, sudoers, cron, systemd, Falco, audit, firewall, boot, or executable paths | Excludes expected cloud-init, bootstrap, and package-manager writes to prevent update floods. |
| **Account or group management tool executed** | User, group, password, or sudoers administration | Provides endpoint-style identity-change auditing. |
| **Package management tool executed** | `apt`, `dpkg`, `pip`, or Snap execution on the immutable host | Identifies runtime software installation or alteration. |
| **Kernel module changed** | Kernel module load or unload syscalls | Detects kernel-level persistence or tampering. |
| **Firewall or routing tool executed** | UFW, nftables, iptables, routes, or traffic-control changes | Excludes expected boot-time firewall setup. |
| **Privilege or namespace tool executed** | Capability, chroot, or namespace manipulation | Detects common privilege-escalation and container-escape primitives. |
| **Execution from writable temporary directory** | Binary execution from `/tmp` or `/var/tmp` | Complements the upstream `/dev/shm` and dropped-container-binary detections. |
| **Network reconnaissance tool executed** | Scanners, sniffers, netcat, Socat, or Telnet | Detects discovery, relays, and common interactive network tooling. |
| **SSM interactive shell started** | Shell descended from the SSM agent/session worker | Audits the server's supported administrative access path. |
| **Sensitive credential file opened** | Unexpected reads of password databases, sudoers, SSH paths, or Dragonwilds runtime secrets | Excludes the small set of expected authentication and container-runtime readers. |

- **Alert Destination**: Structured JSON written directly to `/var/log/falco_alerts.json`.
- **Severity handling**: Informational and higher events are retained for audit searches; CloudWatch alarms remain restricted to Warning and higher.
- **Build validation**: Falco runs with `--dry-run -o engine.kind=nodriver` during baking; configuration or rule errors fail the build.
- **Log Rotation**: Governed by `/etc/logrotate.d/falco-alerts` (daily rotation, 7-day local retention, compressed).

### 2. Log Telemetry (AWS CloudWatch Logs)
The CloudWatch Agent publishes 60-second host, process, and health metrics and collects logs into Log Group **`/${ProjectName}/${Environment}/system`**:
- **`{instance_id}/falco-security`**: Streams from `/var/log/falco_alerts.json`.
- **`{instance_id}/cloud-init`**: Streams from `/var/log/cloud-init-output.log`.
- **Host Metrics**: Native `AWS/EC2` metrics provide CPU and network utilization; namespace `CWAgent` tracks memory and disk utilization on `/` and `/srv/dragonwilds`.
- **Process Metrics**: CloudWatch Agent `procstat` tracks CPU and resident memory for Falco and the Dragonwilds process.
- **Health Metrics**: A systemd timer publishes `game_healthy` and `falco_healthy` gauges to the agent's local StatsD listener. Missing metrics are treated as alarm failures.
- **Dashboard**: CloudFormation creates a regional dashboard for availability, CPU, memory, network, disk, Falco alerts, and recent security events.
- **Automatic host recovery**: A `StatusCheckFailed_System` alarm invokes EC2 recovery after two consecutive failed one-minute checks, preserving the instance identity, Elastic IP, and attached EBS world volume while also notifying the SNS topic.

### 3. File Integrity Monitoring
AIDE is disabled in the CIS variables and purged during finalization. Falco remains the runtime file and process detection control, while AMIs are rebuilt from versioned infrastructure code instead of maintaining an on-host AIDE database.

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

### Persistent Storage & Bootstrap (`dragonwilds-bootstrap.sh`)
- World data is stored on a separate retained gp3 EBS volume mounted at `/srv/dragonwilds`.
- CloudFormation attaches the volume to the EC2 instance as `/dev/sdf`; the guest waits for its NVMe device, creates an ext4 filesystem when new, and mounts it.
- Discovers the EBS volume dynamically by serial number across `/dev/disk/by-id/` and `lsblk` NVMe identifiers.
- Dynamically discovers the filesystem UUID and adds an `/etc/fstab` entry on first bootstrap. If replacing the filesystem on an existing instance with a different UUID, update that entry before rebooting.
- Fetches all ten official `RSDW_*` container settings from AWS Secrets Manager and writes them to ephemeral memory at `/run/dragonwilds/server.env`.
- Can import an existing `.sav` from S3 before the container starts. The selected S3 object is imported once; later reboots keep the live EBS copy. If an old or automatically generated save already exists, it is moved to `RSDragonwilds/Saved/S3ImportBackups/` first.

---

## Building & Deploying

### Prerequisites
```bash
# 1. Install Python dependencies
pip install -r requirements-dev.txt

# 2. Install pinned Ansible roles
ansible-galaxy install -r ansible/requirements.yml

# 3. Authenticate locally with the AWS CLI before building or deploying.
# Keep AWS configuration and credentials outside this repository.
```

Local commands use the AWS SDK credential chain. Choose your AWS profile in your local shell or CLI configuration; CI uses GitHub OIDC and the server uses its EC2 IAM role. Do not commit profile files or exported credentials.

### GitHub Actions CI/CD (Bake & Deploy)

All AMI baking and CloudFormation deployments are automated in GitHub Actions via [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml):

- **Automatic Selection:** On push to `main`, GitHub Actions checks which files changed:
  - Changes to CIS hardening, Packer master templates, or requirements trigger a **Full Master Build** (~25m).
  - Changes to container files, bootstrap scripts, or Falco rules trigger a **Fast Update Build** (~3m).
  - Changes only to CloudFormation trigger **Deploy Only**.
- **Manual Trigger:** Use the **Run workflow** button in GitHub Actions to trigger `auto`, `fast`, `master`, or `deploy-only` builds at any time.
- **Server Size:** The default `t3a.large` provides 8 GiB RAM on x86-64 at a lower price than the equivalent T3 instance. Set the GitHub Actions variable `INSTANCE_TYPE` to override it.

Local manual commands (if debugging directly with Packer or AWS CLI):
```bash
# Optional manual Packer build:
packer init packer/dragonwilds.pkr.hcl
packer build packer/dragonwilds.pkr.hcl

# Nested templates must first be uploaded and rewritten to S3:
aws cloudformation package \
  --template-file cloudformation/server.yaml \
  --s3-bucket <CFN_ARTIFACT_BUCKET> \
  --s3-prefix dragonwilds/cloudformation \
  --output-template-file /tmp/dragonwilds-packaged.yaml

aws cloudformation deploy \
  --stack-name dragonwilds \
  --template-file /tmp/dragonwilds-packaged.yaml \
  --parameter-overrides AmiId=<AMI_ID> ServerSecretArn=<SERVER_SECRET_ARN> \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --no-fail-on-empty-changeset
```

Set the GitHub Actions secret `CFN_ARTIFACT_BUCKET` to an existing private S3 bucket used to package nested templates. For infrastructure-only updates, CI preserves the existing stack's AMI and secret ARN. `BASE_AMI_ID` is only a fallback before the first stack exists. All ten secret fields must be JSON strings, and line breaks or NUL bytes cannot be represented in Podman's environment-file format.

Moving an already-deployed monolithic stack to nested stacks is a resource migration, not an in-place rearrangement. Preserve or back up the retained world-data volume before the first nested deployment, and test the change set in a non-production stack; fixed-name resources such as the log group and backup vault may need import or a staged rename.

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
aws cloudformation deploy \
  --stack-name dragonwilds \
  --template-file /tmp/dragonwilds-packaged.yaml \
  --parameter-overrides \
    AmiId=<NEW_AMI_ID> \
    ServerSecretArn=<SERVER_SECRET_ARN> \
    WorldSaveBucket=<EXISTING_BUCKET_NAME> \
    WorldSaveKey=<PATH/TO/WORLD.sav> \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --no-fail-on-empty-changeset
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
- CloudFormation owns the data-volume attachment. During an instance replacement, AWS must detach the retained volume from the old instance before attaching it to the replacement; inspect the change set and stop the old server cleanly to avoid a `VolumeInUse` failure.
- `dragonwilds-bootstrap.service` must finish as `active (exited)` before `dragonwilds.service` can start. Use `journalctl -u dragonwilds-bootstrap.service` to identify malformed or missing secret fields.
- The selected filename must match `RSDW_WORLD_NAME` on Linux, where capitalization matters. Check `/run/dragonwilds/server.env` and `RSDragonwilds/Saved/SaveGames/` before starting the container.

When updating the AMI or replacing the instance, cleanly stop the server first to ensure memory buffers are flushed to EBS:

```bash
INSTANCE_ID=$(aws cloudformation describe-stack-resources \
  --stack-name dragonwilds \
  --logical-resource-id WorkloadStack \
  --query "StackResourceDetail.PhysicalResourceId" --output text \
| xargs -I{} aws cloudformation describe-stacks --stack-name {} \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
aws ec2 stop-instances --instance-ids "$INSTANCE_ID"
aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
# Run the deploy-only GitHub Actions workflow after the instance stops.
```

### Local checks

```bash
pip install -r requirements-dev.txt
pre-commit install --install-hooks

# Run the same quality gates as CI, including the regression tests:
python -m pre_commit run --all-files --hook-stage pre-commit

# Run the slower pre-push security hooks explicitly:
python -m pre_commit run --all-files --hook-stage pre-push
```

The pre-push hooks use the locally installed Trivy CLI to scan both this
repository and `ghcr.io/runescape/rsdw-dedicated:latest`. They do not require
Docker; the server continues to use Podman and Quadlets.

The tests verify CIS prerequisites and exact secret formatting. They do not deploy resources. A real AMI bake and replacement are still needed to verify the complete EC2 boot path.

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
