# dragonwilds-aws

Infrastructure-as-code and runtime security for hosting a hardened [RuneScape: Dragonwilds](https://store.steampowered.com/app/1374490/RuneScape_Dragonwilds/) dedicated server on AWS EC2.

---

## Architecture Overview

A hardened Ubuntu 24.04 AMI is built with Packer and Ansible, then deployed via CloudFormation. The game server runs as a system Podman container managed by systemd via a Quadlet unit file, continuously monitored at the kernel level by Falco (eBPF) and File Integrity Monitoring (AIDE).

```
Packer
  ├── image.yml               # Installs Podman, pre-pulls container image, configures Falco, purges bloat
  └── harden.yml              # Applies CIS Level 1 hardening (ansible-lockdown role)
        └── vars/cis.yml      # Benchmark overrides for containerized EC2 host

CloudFormation
  └── server.yaml             # VPC, EC2, UFW, EBS world volume, IAM roles, CloudWatch Logs

Runtime Stack
  ├── Telemetry & Monitoring
  │     ├── Falco (modern eBPF kernel tracing -> /var/log/falco_alerts.json)
  │     ├── CloudWatch Agent (streams falco-security & cloud-init to CloudWatch Logs)
  │     └── AIDE (SHA256 baseline file integrity monitoring)
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
│   ├── requirements.yml               # Pinned ansible-lockdown dependencies
│   ├── vars/
│   │   └── cis.yml                    # CIS benchmark overrides
│   └── roles/
│       └── ubuntu24_cis/              # Pinned UBUNTU24-CIS role
├── cloudformation/
│   └── server.yaml                    # VPC, EC2, IAM, EBS, UFW routing, CloudWatch Agent
├── files/
│   ├── dragonwilds.container          # Podman Quadlet unit file (zero-dependency health check)
│   ├── dragonwilds-bootstrap.service  # Reboot-safe volume mount & secret injection
│   ├── dragonwilds-bootstrap.sh       # NVMe EBS dynamic attachment & secret retrieval
│   ├── falco-dragonwilds.yaml         # Falco JSON file output & rule whitelist
│   └── falco-dragonwilds-rules.yaml   # Custom Falco security rules (tampering, sudo, auth, etc.)
└── packer/
    ├── dragonwilds.pkr.hcl            # Full production AMI build (CIS + runtime, ~25m)
    ├── dragonwilds-update.pkr.hcl     # Fast incremental build on base AMI (~90s)
    └── cis-audit.pkr.hcl              # Non-mutating CIS audit scanner
```

---

## Security Architecture

### 1. Runtime Kernel Detection (Falco eBPF)
Falco runs via the `modern_ebpf` driver (`falco-modern-bpf.service`) without requiring kernel headers or compiler toolchains. All default noisy rules are disabled, evaluating only **5 targeted security rules**:

| Rule | Target Trigger | Mitigation / Tuning |
| :--- | :--- | :--- |
| **Dragonwilds data directory modified** | Detects writes, unlinks, or renames in `/srv/dragonwilds` or `/home/steam/rsdw-dedicated` | Excludes legitimate `RSDragonwildsSe` game saves to eliminate alert noise while alerting on any external tampering. |
| **Sudo execution** | Execution of `sudo` anywhere on the host or inside containers | Alerts whenever privilege escalation occurs. |
| **Authentication attempt failed** | Writes to `/var/log/btmp` | Detects failed SSH, console, or PAM login attempts. |
| **Use of chattr** | Execution of `chattr` or `lsattr` | Detects attempts to set immutable flags (`+i`) for defense evasion. |
| **Use of Python** | Execution of `python`, `python3`, `python3.12` | Detects interactive scripts or living-off-the-land execution. |

- **Alert Destination**: Structured JSON written directly to `/var/log/falco_alerts.json`.
- **Log Rotation**: Governed by `/etc/logrotate.d/falco-alerts` (daily rotation, 7-day local retention, compressed).

### 2. Log Telemetry (AWS CloudWatch Logs)
The CloudWatch Agent collects logs and flushes batches every 60 seconds into Log Group **`/${ProjectName}/${Environment}/system`**:
- **`{instance_id}/falco-security`**: Streams from `/var/log/falco_alerts.json`.
- **`{instance_id}/cloud-init`**: Streams from `/var/log/cloud-init-output.log`.
- **Host Metrics**: Namespace `CWAgent` tracks CPU, memory, and disk utilization on `/` and `/srv/dragonwilds`.

### 3. File Integrity Monitoring (AIDE)
- A baseline cryptographic database (`/var/lib/aide/aide.db`, ~26 MB) is generated during the initial CIS build.
- Periodic scans verify filesystem binaries, libraries, and configuration files against the SHA256 baseline.

### 4. Network & Host Hardening
- **Zero Inbound SSH**: TCP port 22 is disabled. Management access is strictly via **AWS Systems Manager (SSM) Session Manager**.
- **Strict UFW + Routing**: UFW defaults to deny incoming. Ports `7777/udp` and `8888/udp` are allowed inbound, and `ufw route allow` rules are explicitly enabled to allow Podman bridged NAT forwarding without dropped packets.
- **IMDSv2 Enforced**: Metadata hop limit set to 1, preventing container processes from accessing the EC2 instance role credentials.
- **Package Minimization**: Unnecessary bloat and potential living-off-the-land tools are purged during image baking:
  `git`, `ssh-import-id`, `ubuntu-drivers-common`, `usbutils`, `bpftrace`, `snapd`, `open-iscsi`, `needrestart`, `landscape-common`, `apport`, `unattended-upgrades`.

---

## Container & World Storage

### Quadlet Service (`dragonwilds.container`)
- Managed natively by systemd (`systemd/generator`).
- **Pre-Baked Image**: The pinned image digest (`ghcr.io/runescape/rsdw-dedicated@sha256:a35b...`) is pre-pulled directly into the AMI during image baking, eliminating multi-minute image downloads on instance boot.
- **Zero-Dependency Health Check**: Uses direct procfs UDP table inspection (`grep -qi ':1E61 ' /proc/net/udp || grep -qi ':1E61 ' /proc/net/udp6`), removing dependencies on `ss` or `netstat`.
- Restarts automatically (`Restart=always`, `RestartSec=15`).
- Initial SteamCMD download window supported via `TimeoutStartSec=900`.

### Dynamic Storage & Bootstrap (`dragonwilds-bootstrap.sh`)
- World data is stored on a separate retained gp3 EBS volume mounted at `/srv/dragonwilds`.
- Discovers the EBS volume dynamically by serial number across `/dev/disk/by-id/` and `lsblk` NVMe identifiers.
- Dynamically discovers the filesystem UUID and injects an idempotent `/etc/fstab` entry, preventing UUID collision failures when restoring AWS Backup snapshots.
- Fetches container secrets (`RSDW_OWNER_ID`, `RSDW_WORLD_NAME`, `RSDW_PASSWORD`, `RSDW_ADMIN_PASSWORD`) from AWS Secrets Manager and writes them to ephemeral memory at `/run/dragonwilds/server.env`.

---

## Building & Deploying

### Prerequisites
```bash
# 1. Install pinned Ansible roles
ansible-galaxy install -r ansible/requirements.yml

# 2. Export active AWS credentials (AWS SSO example)
eval "$(aws configure export-credentials --profile jayadmin --format env)"
```

### Option A: Fast AMI Layering (~90 seconds)
If you already have a hardened base AMI, bake runtime, container pre-pull, and Falco updates quickly:
```bash
packer build \
  -var "base_ami_id=ami-0d65645d21f30bae1" \
  -var "aws_profile=jayadmin" \
  packer/dragonwilds-update.pkr.hcl
```

### Option B: Full AMI Build (~25 minutes)
Builds the base OS, applies the full CIS Level 1 benchmark, and layers the runtime stack:
```bash
packer build packer/dragonwilds.pkr.hcl
```

### Deploying the CloudFormation Stack
```bash
aws cloudformation deploy \
  --template-file cloudformation/server.yaml \
  --stack-name dragonwilds \
  --parameter-overrides \
    AmiId=<NEW_AMI_ID> \
    ServerSecretArn=<SECRETS_MANAGER_ARN> \
  --capabilities CAPABILITY_IAM \
  --profile jayadmin
```

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
  --log-stream-name "$(aws ec2 describe-instances --filters "Name=tag:Name,Values=dragonwilds-production-server" "Name=instance-state-name,Values=running" --query "Reservations[0].Instances[0].InstanceId" --output text --profile jayadmin)/falco-security" \
  --limit 5 \
  --profile jayadmin
```

### Run File Integrity Check (AIDE)
```bash
sudo aide --config /etc/aide/aide.conf --check
```
