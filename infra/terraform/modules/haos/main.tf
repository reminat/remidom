resource "proxmox_virtual_environment_vm" "vm" {
  node_name = var.pm_node
  name      = var.vm_name

  clone {
    vm_id = var.vm_template_id
  }

  cpu {
    cores = var.vm_cores
    type  = "host"
  }

  memory {
    dedicated = var.vm_memory_mb
  }

  disk {
    datastore_id = var.vm_storage
    interface    = "scsi0"
    size         = var.vm_disk_gb
  }

  network_device {
    bridge = var.vm_bridge
  }

  started = true
  on_boot = true
}
