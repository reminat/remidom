provider "proxmox" {
  endpoint  = var.pm_endpoint
  api_token = "${var.pm_api_token_id}=${var.pm_api_token_secret}"
  insecure  = var.pm_tls_insecure
  ssh {
    agent       = false
    username    = "root"
    private_key = file(pathexpand("~/.ssh/id_ed25519_pve"))
  }
}

module "docker_host" {
  source = "../../../../modules/vm"

  pm_node                 = var.pm_node
  vm_name                 = var.vm_name
  vm_template_id          = var.vm_template_id
  vm_cores                = var.vm_cores
  vm_memory_mb            = var.vm_memory_mb
  vm_disk_gb              = var.vm_disk_gb
  vm_storage              = var.vm_storage
  vm_bridge               = var.vm_bridge
  ssh_authorized_keys     = var.ssh_authorized_keys
  user_data_template_path = "${path.root}/../../../../../proxmox/snippets/docker-host.user-data.yaml.tftpl"
}
