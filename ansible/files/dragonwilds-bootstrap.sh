#!/usr/bin/bash

set -euo pipefail

: "${AWS_REGION:?AWS_REGION is required}"
: "${DATA_VOLUME_ID:?DATA_VOLUME_ID is required}"
: "${SERVER_SECRET_ARN:?SERVER_SECRET_ARN is required}"

volume_serial="$(printf '%s' "$DATA_VOLUME_ID" | tr -d '-')"
volume_device=""

for _ in $(seq 1 60); do
  by_id="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${volume_serial}"

  if [[ -e "$by_id" ]]; then
    volume_device="$(readlink -f "$by_id")"
    break
  fi

  volume_device="$(lsblk -ndo PATH,SERIAL | awk -v serial="$volume_serial" '$2 == serial { print $1; exit }')"
  if [[ -n "$volume_device" ]]; then
    break
  fi

  if [[ -b /dev/xvdf ]]; then
    volume_device=/dev/xvdf
    break
  fi

  sleep 5
done

if [[ -z "$volume_device" || ! -b "$volume_device" ]]; then
  echo "Dragonwilds data volume $DATA_VOLUME_ID was not found" >&2
  exit 1
fi

if ! blkid "$volume_device" >/dev/null 2>&1; then
  mkfs.ext4 -L dragonwilds "$volume_device"
fi

filesystem_uuid="$(blkid -s UUID -o value "$volume_device")"
install -d -o root -g root -m 0755 /srv/dragonwilds

if ! grep -qE '^[^#]+[[:space:]]+/srv/dragonwilds[[:space:]]' /etc/fstab; then
  printf 'UUID=%s /srv/dragonwilds ext4 defaults,nofail 0 2\n' "$filesystem_uuid" >> /etc/fstab
fi

mountpoint -q /srv/dragonwilds || mount /srv/dragonwilds
chown 1000:1000 /srv/dragonwilds
chmod 0750 /srv/dragonwilds

secret_json=""
for _ in $(seq 1 12); do
  if secret_json="$(aws secretsmanager get-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$SERVER_SECRET_ARN" \
    --query SecretString \
    --output text)"; then
    break
  fi
  sleep 5
done

if [[ -z "$secret_json" ]]; then
  echo "Unable to retrieve the Dragonwilds server secret" >&2
  exit 1
fi

install -d -o root -g root -m 0700 /run/dragonwilds
environment_file="$(mktemp /run/dragonwilds/server.env.XXXXXX)"
trap 'rm -f "$environment_file"' EXIT

printf '%s' "$secret_json" | jq -er '
  ["RSDW_OWNER_ID", "RSDW_WORLD_NAME", "RSDW_PASSWORD", "RSDW_ADMIN_PASSWORD"] as $required
  | if type != "object" then error("secret must be a JSON object") else . end
  | $required[] as $key
  | .[$key] as $value
  | if ($value | type) != "string" then
      error("secret key must be a string: " + $key)
    elif ($value | explode | any(. == 0 or . == 10 or . == 13)) then
      error("secret key contains an unsupported line break or NUL: " + $key)
    else "\($key)=\($value)"
    end
' > "$environment_file"

chown root:root "$environment_file"
chmod 0600 "$environment_file"
mv -f "$environment_file" /run/dragonwilds/server.env
