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

world_save_bucket="${WORLD_SAVE_BUCKET:-}"
world_save_key="${WORLD_SAVE_KEY:-}"

if [[ -n "$world_save_bucket" || -n "$world_save_key" ]]; then
  if [[ -z "$world_save_bucket" || -z "$world_save_key" ]]; then
    echo "WORLD_SAVE_BUCKET and WORLD_SAVE_KEY must both be set" >&2
    exit 1
  fi

  save_root=/srv/dragonwilds/RSDragonwilds/Saved
  save_directory="$save_root/SaveGames"
  import_source="s3://$world_save_bucket/$world_save_key"
  import_marker="$save_root/.s3-world-source"
  imported_source=""
  if [[ -f "$import_marker" ]]; then
    imported_source="$(cat "$import_marker")"
  fi

  if [[ "$imported_source" != "$import_source" ]]; then
    install -d -o 1000 -g 1000 -m 0750 "$save_directory"
    world_filename="${world_save_key##*/}"
    if [[ -z "$world_filename" || "$world_filename" != *.sav ]]; then
      echo "WORLD_SAVE_KEY must identify a .sav file" >&2
      exit 1
    fi

    staged_world="$(mktemp "$save_root/.world-import.XXXXXX")"
    import_marker_tmp="$(mktemp "$save_root/.s3-world-source.XXXXXX")"
    trap 'rm -f "${environment_file:-}" "$staged_world" "$import_marker_tmp"' EXIT
    for _ in $(seq 1 12); do
      if aws s3api get-object \
        --region "$AWS_REGION" \
        --bucket "$world_save_bucket" \
        --key "$world_save_key" \
        "$staged_world" >/dev/null; then
        break
      fi
      sleep 5
    done
    if [[ ! -s "$staged_world" ]]; then
      echo "Unable to download non-empty world save $import_source" >&2
      exit 1
    fi

    if find "$save_directory" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
      backup_directory="$save_root/S3ImportBackups/$(date -u +%Y%m%dT%H%M%SZ)"
      install -d -o 1000 -g 1000 -m 0750 "$backup_directory"
      find "$save_directory" -mindepth 1 -maxdepth 1 -exec mv -t "$backup_directory" -- {} +
    fi

    chown 1000:1000 "$staged_world"
    chmod 0640 "$staged_world"
    mv -f "$staged_world" "$save_directory/$world_filename"
    printf '%s\n' "$import_source" > "$import_marker_tmp"
    chown 1000:1000 "$import_marker_tmp"
    chmod 0640 "$import_marker_tmp"
    mv -f "$import_marker_tmp" "$import_marker"
    echo "Imported initial Dragonwilds world from $import_source"
  fi
fi

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
trap 'rm -f "$environment_file" "${staged_world:-}" "${import_marker_tmp:-}"' EXIT

printf '%s' "$secret_json" | jq -er '
  [
    "RSDW_OWNER_ID", "RSDW_PORT", "RSDW_BEACON_PORT", "RSDW_SERVER_NAME",
    "RSDW_WORLD_NAME", "RSDW_PASSWORD", "RSDW_ADMINS", "RSDW_ADMIN_PASSWORD",
    "RSDW_ADDITIONAL_ARGS", "RSDW_AUTO_STOP_ON_UPDATE"
  ] as $required
  | [
      "RSDW_OWNER_ID", "RSDW_PORT", "RSDW_BEACON_PORT", "RSDW_SERVER_NAME",
      "RSDW_WORLD_NAME", "RSDW_ADMINS", "RSDW_ADMIN_PASSWORD",
      "RSDW_AUTO_STOP_ON_UPDATE"
    ] as $nonempty
  | if type != "object" then error("secret must be a JSON object") else . end
  | $required[] as $key
  | .[$key] as $value
  | if ($value | type) != "string" then
      error("secret key must be a string: " + $key)
    elif (($nonempty | index($key)) != null and ($value | length) == 0) then
      error("secret key must not be empty: " + $key)
    elif ($key == "RSDW_PORT" and $value != "7777") then
      error("RSDW_PORT must be 7777 to match the published game port")
    elif ($key == "RSDW_BEACON_PORT" and $value != "8888") then
      error("RSDW_BEACON_PORT must be 8888 to match the published beacon port")
    elif ($key == "RSDW_AUTO_STOP_ON_UPDATE" and ($value != "true" and $value != "false")) then
      error("RSDW_AUTO_STOP_ON_UPDATE must be true or false")
    elif ($value | explode | any(. == 0 or . == 10 or . == 13)) then
      error("secret key contains an unsupported line break or NUL: " + $key)
    else "\($key)=\($value)"
    end
' > "$environment_file"

chown root:root "$environment_file"
chmod 0600 "$environment_file"
mv -f "$environment_file" /run/dragonwilds/server.env
