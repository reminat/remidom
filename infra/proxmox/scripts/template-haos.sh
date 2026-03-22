#!/usr/bin/env bash
# Creates Proxmox template 9100: Home Assistant OS (latest release)
# Run directly on the Proxmox host as root.
set -euo pipefail

VMID=9100
NAME=tpl-haos
STORAGE=local-lvm
BRIDGE=vmbr0
MEM=4096
CORES=2

cd /tmp

echo "[template-haos] Resolving latest HAOS release..."
LATEST_URL="$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/home-assistant/operating-system/releases/latest)"
VERSION="${LATEST_URL##*/}"
echo "[template-haos] Version: ${VERSION}"

IMG_XZ="haos_ova-${VERSION}.qcow2.xz"
IMG_QCOW2="haos_ova-${VERSION}.qcow2"

rm -f "${IMG_XZ}" "${IMG_QCOW2}"

echo "[template-haos] Downloading HAOS image..."
wget -O "${IMG_XZ}" "https://github.com/home-assistant/operating-system/releases/download/${VERSION}/${IMG_XZ}"
unxz -f "${IMG_XZ}"

if qm status "${VMID}" >/dev/null 2>&1; then
  echo "[template-haos] Existing VM ${VMID} found, purging..."
  qm stop "${VMID}" >/dev/null 2>&1 || true
  qm destroy "${VMID}" --purge
fi

echo "[template-haos] Creating VM ${VMID}..."
qm create "${VMID}" \
  --name "${NAME}" \
  --memory "${MEM}" \
  --cores "${CORES}" \
  --net0 "virtio,bridge=${BRIDGE}" \
  --ostype l26

qm set "${VMID}" --machine q35 --bios ovmf

echo "[template-haos] Importing disk..."
qm importdisk "${VMID}" "${IMG_QCOW2}" "${STORAGE}"

IMPORTED="$(qm config "${VMID}" | awk '/^unused0:/ {print $2}')"
if [ -z "${IMPORTED}" ]; then
  echo "ERROR: imported disk not found in unused0 (qm config ${VMID})." >&2
  qm config "${VMID}" >&2
  exit 1
fi

echo "[template-haos] Configuring VM..."
qm set "${VMID}" --scsihw virtio-scsi-pci --scsi0 "${IMPORTED}"
qm set "${VMID}" --delete unused0
qm set "${VMID}" --efidisk0 "${STORAGE}:0,pre-enrolled-keys=0"
qm set "${VMID}" --boot order=scsi0
qm set "${VMID}" --serial0 socket --vga serial0
qm set "${VMID}" --agent enabled=1

echo "[template-haos] Converting to template..."
qm template "${VMID}"

echo "[template-haos] Template OK: VMID=${VMID}, version=${VERSION}, disk=${IMPORTED}"
