provider "proxmox" {
  endpoint  = var.pm_endpoint
  api_token = "${var.pm_api_token_id}=${var.pm_api_token_secret}"
  insecure  = var.pm_tls_insecure
  ssh {
    agent    = true
    username = "root"
  }
}

resource "proxmox_virtual_environment_vm" "haos" {
  node_name = var.pm_node
  name      = var.haos_vm_name

  clone {
    vm_id = var.haos_template_id
  }

  cpu {
    cores = var.haos_cores
    type  = "host"
  }

  memory {
    dedicated = var.haos_memory_mb
  }

  disk {
    datastore_id = var.vm_storage
    interface    = "scsi0"
    size         = var.haos_disk_gb
  }

  network_device {
    bridge = var.vm_bridge
  }

  started = true
  on_boot = true
}
