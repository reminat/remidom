#!/usr/bin/env bash
# Creates a Proxmox template: Ubuntu 24.04 LTS cloud image, UEFI (OVMF) + q35, cloud-init.
# Run directly on the Proxmox host as root:
#   bash template-ubuntu-2404.sh <VMID>
#
# Does NOT replace template 9000 (Ubuntu 22.04, template-docker.sh) used by the Docker hosts.
# Never destroys or modifies an existing VM: an occupied VMID or name aborts the script.
# The source VM is never started; it is converted to a template at the end.
#
# Image: dated release (not "current"), checked against a pinned SHA-256.
# Provenance of the pinned checksum (checked on 05/11/2026):
#   https://cloud-images.ubuntu.com/releases/noble/release-20261017/SHA256SUMS
#   signed by SHA256SUMS.gpg, "UEC Image Automatic Signing Key <cdimage@ubuntu.com>",
#   key D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81 (ubuntu-cloudimage-keyring).
# To move to a newer release, update RELEASE and IMG_SHA256 together after the same check
# (see infra/terraform/DEV-AGENT.md).
#
# Optional overrides (environment): TEMPLATE_NAME, STORAGE, BRIDGE, WORKDIR.
#
# Local writes on the host, before any Proxmox change: a lock file (/run/lock) and the
# verified image cache (WORKDIR). Proxmox resources are only changed in the last section.
set -euo pipefail

RELEASE=20261017
IMG=ubuntu-24.04-server-cloudimg-amd64.img
IMG_SHA256=04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94
IMG_URL="https://cloud-images.ubuntu.com/releases/noble/release-${RELEASE}/${IMG}"

VMID="${1:-}"
NAME="${TEMPLATE_NAME:-tpl-ubuntu-2404-cloudinit}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
WORKDIR="${WORKDIR:-/var/lib/vz/template/cache}"
MEM=2048
CORES=2

log() { echo "[template-ubuntu-2404] $*"; }
die() { echo "[template-ubuntu-2404] ERROR: $*" >&2; exit 1; }

# --- Checks (no Proxmox resource is changed in this section) ----------------

[ -n "${VMID}" ] || die "usage: $0 <VMID>  (VMID is required, choose it after checking Proxmox)"
[[ "${VMID}" =~ ^[1-9][0-9]{2,8}$ ]] || die "invalid VMID '${VMID}' (expected an integer >= 100)"
[ "$(id -u)" -eq 0 ] || die "must run as root on the Proxmox host"
[ "$#" -eq 1 ] || die "usage: $0 <VMID>"
[[ "$NAME" =~ ^[A-Za-z][A-Za-z0-9-]*$ ]] || die "invalid template name"
[[ "$STORAGE" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] || die "invalid storage name"
[[ "$BRIDGE" =~ ^[A-Za-z][A-Za-z0-9_.-]*$ ]] || die "invalid bridge name"
[[ "$WORKDIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || die "invalid work directory"
for cmd in qm pct pvesh pvesm sha256sum wget awk grep mktemp flock; do
  command -v "${cmd}" >/dev/null 2>&1 || die "missing command: ${cmd}"
done

# Global lock: one run of this script at a time on this host, whatever the VMID or name.
# It does not protect against other tools, the Proxmox web interface or another node.
exec 9>"/run/lock/remidom-template-ubuntu-2404.lock"
flock -n 9 || die "another run of this script is in progress on this host"

# VMID must be free cluster-wide (VM or container). pvesh fails if the ID is in use.
if qm status "${VMID}" >/dev/null 2>&1 || pct status "${VMID}" >/dev/null 2>&1 \
   || ! pvesh get /cluster/nextid --vmid "${VMID}" >/dev/null 2>&1; then
  die "VMID ${VMID} is not available. Nothing was changed in Proxmox.
Inspect the existing guest ('qm config ${VMID}' or 'pct config ${VMID}') before choosing another VMID."
fi

# Refuse a duplicate template name.
if qm list | awk 'NR>1 {print $2}' | grep -qxF "${NAME}"; then
  die "a VM named '${NAME}' already exists. Nothing was changed in Proxmox. Inspect it ('qm list')."
fi

pvesm status --storage "${STORAGE}" >/dev/null 2>&1 || die "storage '${STORAGE}' not found"
pvesm status --storage "${STORAGE}" --content images 2>/dev/null | awk 'NR>1 && $3 == "active" { found=1 } END { exit !found }' \
  || die "storage '${STORAGE}' does not accept VM disk images"
[ -d "/sys/class/net/${BRIDGE}/bridge" ] || die "bridge '${BRIDGE}' not found"
[ -d "${WORKDIR}" ] || die "work directory '${WORKDIR}' not found"

log "VMID=${VMID} name=${NAME} storage=${STORAGE} bridge=${BRIDGE} release=${RELEASE}"

# --- Image: download if needed, always verify --------------------------------

IMG_PATH="${WORKDIR}/ubuntu-24.04-server-cloudimg-amd64-${RELEASE}.img"

if [ -f "${IMG_PATH}" ] && echo "${IMG_SHA256}  ${IMG_PATH}" | sha256sum --check --status; then
  log "Image already present and verified: ${IMG_PATH}"
else
  [ ! -e "${IMG_PATH}" ] || die "${IMG_PATH} exists but its checksum does not match. Remove it manually after checking, then rerun."
  PART="$(mktemp "${IMG_PATH}.part.XXXXXX")"
  trap 'rm -f "${PART}"' EXIT
  log "Downloading ${IMG_URL}"
  wget -q -O "${PART}" "${IMG_URL}"
  echo "${IMG_SHA256}  ${PART}" | sha256sum --check --status \
    || die "checksum mismatch for the downloaded image; nothing was created"
  mv "${PART}" "${IMG_PATH}"
  trap - EXIT
  log "Image verified: ${IMG_PATH}"
fi

# --- Template creation -------------------------------------------------------

# Proxmox resources are changed from here on. Nothing is removed automatically on failure.
pvesh get /cluster/nextid --vmid "${VMID}" >/dev/null 2>&1 \
  || die "VMID ${VMID} is no longer available. Nothing was changed in Proxmox."

trap 'echo "[template-ubuntu-2404] ERROR: a Proxmox command failed. Nothing was removed. Inspect VMID ${VMID} (qm config ${VMID}) before any action." >&2' ERR

log "Creating VM ${VMID} (not started)..."
qm create "${VMID}" \
  --name "${NAME}" \
  --description "Ubuntu 24.04 cloud image release-${RELEASE}, sha256 ${IMG_SHA256}" \
  --onboot 0 \
  --ostype l26 \
  --machine q35 \
  --bios ovmf \
  --memory "${MEM}" \
  --cores "${CORES}" \
  --net0 "virtio,bridge=${BRIDGE}" \
  --scsihw virtio-scsi-pci \
  --agent enabled=1 \
  --serial0 socket \
  --vga serial0

log "Adding EFI disk (4m, no pre-enrolled keys)..."
qm set "${VMID}" --efidisk0 "${STORAGE}:0,efitype=4m,pre-enrolled-keys=0"

log "Importing system disk..."
qm set "${VMID}" --scsi0 "${STORAGE}:0,import-from=${IMG_PATH}"

log "Adding cloud-init drive and boot order..."
qm set "${VMID}" --ide2 "${STORAGE}:cloudinit"
qm set "${VMID}" --boot order=scsi0

log "Converting to template..."
qm template "${VMID}"

log "Template OK: VMID=${VMID} name=${NAME}"
