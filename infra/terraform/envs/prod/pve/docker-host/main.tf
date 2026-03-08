provider "proxmox" {
  endpoint  = var.pm_endpoint
  api_token = "${var.pm_api_token_id}=${var.pm_api_token_secret}"
  insecure  = var.pm_tls_insecure
  ssh {
    agent    = false
    username = "root"
    private_key = file(pathexpand("~/.ssh/id_ed25519_pve"))
  }
}

resource "proxmox_virtual_environment_vm" "docker_host" {
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
    user_data_file_id = proxmox_virtual_environment_file.docker_host_user_data.id
  }

  started = true
  on_boot = true
}

resource "proxmox_virtual_environment_file" "docker_host_user_data" {
  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.pm_node

  source_raw {
    data = templatefile("${path.module}/../../../../../proxmox/snippets/docker-host.user-data.yaml.tftpl", {
      hostname            = var.vm_name
      ssh_authorized_keys = var.ssh_authorized_keys
    })
    file_name = "docker-host.user-data.yaml"
  }
}
