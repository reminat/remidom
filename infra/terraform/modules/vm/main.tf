resource "proxmox_virtual_environment_vm" "vm" {
  node_name = var.pm_node
  name      = var.vm_name

  clone {
    vm_id = var.vm_template_id
  }

  agent {
    enabled = true
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

  initialization {
    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }
    user_data_file_id = proxmox_virtual_environment_file.user_data.id
  }

  started = true
  on_boot = true
}

resource "proxmox_virtual_environment_file" "user_data" {
  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.pm_node

  source_raw {
    data = templatefile(var.user_data_template_path, {
      hostname            = var.vm_name
      ssh_authorized_keys = var.ssh_authorized_keys
    })
    file_name = "${var.vm_name}.user-data.yaml"
  }
}
