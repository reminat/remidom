#!/usr/bin/env bash
set -euo pipefail

# Wrapper Terraform: hydrate required creds from Bitwarden (bw + bws) without storing them in terraform.tfvars

# 1) Ensure bw session is unlocked (interactive once per shell)
if ! bw status >/dev/null 2>&1; then
  echo "bw not found or not configured" >&2
  exit 1
fi

# If already unlocked, keep it. Otherwise unlock.
if [ "$(bw status | python3 -c 'import sys, json; print(json.load(sys.stdin).get("status",""))')" != "unlocked" ]; then
  export BW_SESSION="$(bw unlock --raw)"
fi

# 2) Ensure we have a Secrets Manager access token for bws.
# Store this token in Bitwarden PM as an item named: bws_machine_token
if [ -z "${BWS_ACCESS_TOKEN:-}" ]; then
  export BWS_ACCESS_TOKEN="$(bw get password bws_machine_token)"
fi

# 3) Fetch Proxmox credentials from Bitwarden Secrets Manager.
# Choose ONE of the blocks below depending on the Proxmox Terraform provider you use.

# --- bpg/proxmox provider: expects PROXMOX_VE_API_TOKEN (and often PROXMOX_VE_ENDPOINT too)
# Docs: provider args can be configured via env vars; api_token via PROXMOX_VE_API_TOKEN.
# If you already set endpoint elsewhere, keep it. Otherwise set it here.
if [ -z "${PROXMOX_VE_API_TOKEN:-}" ]; then
  export PROXMOX_VE_API_TOKEN="$(bws secret get PROXMOX_VE_API_TOKEN --value)"
fi

# Optional: if you also store endpoint in SM
# export PROXMOX_VE_ENDPOINT="$(bws secret get PROXMOX_VE_ENDPOINT --value)"

# --- Telmate/Terraform-for-Proxmox providers: expect PM_API_TOKEN_ID + PM_API_TOKEN_SECRET (+ PM_API_URL)
# Uncomment if you use that provider instead of bpg/proxmox.
# export PM_API_URL="$(bws secret get PM_API_URL --value)"
# export PM_API_TOKEN_ID="$(bws secret get PM_API_TOKEN_ID --value)"
# export PM_API_TOKEN_SECRET="$(bws secret get PM_API_TOKEN_SECRET --value)"

# 4) Run terraform
exec terraform "$@"