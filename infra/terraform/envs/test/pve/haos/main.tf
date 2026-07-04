provider "proxmox" {
  endpoint  = var.pm_endpoint
  api_token = "${var.pm_api_token_id}=${var.pm_api_token_secret}"
  insecure  = var.pm_tls_insecure
  ssh {
    agent       = false
    username    = "root"
    private_key = file(pathexpand("~/.ssh/id_ed25519_pve"))
    node {
      name    = var.pm_node
      address = regex("https?://([^:/]+)", var.pm_endpoint)[0]
    }
  }
}

module "haos" {
  source = "../../../../modules/haos"

  pm_node        = var.pm_node
  vm_name        = var.vm_name
  vm_template_id = var.vm_template_id
  vm_cores       = var.vm_cores
  vm_memory_mb   = var.vm_memory_mb
  vm_disk_gb     = var.vm_disk_gb
  vm_storage     = var.vm_storage
  vm_bridge      = var.vm_bridge
}
