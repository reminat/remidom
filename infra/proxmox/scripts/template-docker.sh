#!/usr/bin/env bash
# Creates Proxmox template 9000: Ubuntu 22.04 cloud-init
# Run directly on the Proxmox host as root.
set -euo pipefail

VMID=9000
NAME=tpl-ubuntu-2204-cloudinit
STORAGE=local-lvm
BRIDGE=vmbr0
MEM=2048
CORES=2
IMG=jammy-server-cloudimg-amd64.img
IMG_URL="https://cloud-images.ubuntu.com/jammy/current/${IMG}"

cd /tmp

echo "[template-docker] Downloading Ubuntu 22.04 cloud image..."
wget -O "${IMG}" "${IMG_URL}"

echo "[template-docker] Creating VM ${VMID}..."
qm create "${VMID}" \
  --name "${NAME}" \
  --memory "${MEM}" \
  --cores "${CORES}" \
  --net0 "virtio,bridge=${BRIDGE}"

echo "[template-docker] Importing disk..."
qm importdisk "${VMID}" "${IMG}" "${STORAGE}"

echo "[template-docker] Configuring VM..."
qm set "${VMID}" --scsihw virtio-scsi-pci --scsi0 "${STORAGE}:vm-${VMID}-disk-0"
qm set "${VMID}" --ide2 "${STORAGE}:cloudinit"
qm set "${VMID}" --boot c --bootdisk scsi0
qm set "${VMID}" --serial0 socket --vga serial0
qm set "${VMID}" --agent enabled=1

echo "[template-docker] Converting to template..."
qm template "${VMID}"

echo "[template-docker] Template OK: VMID=${VMID}"
