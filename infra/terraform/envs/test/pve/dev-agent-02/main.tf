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

# Poste de travail de validation (Ubuntu 24.04, UEFI/q35). Voir ../../../../DEV-AGENT.md.
# Pas de tf.conf : tf.sh ne construit pas le template, il est préparé à part.
module "dev_agent" {
  source = "../../../../modules/vm"

  pm_node                 = var.pm_node
  vm_name                 = var.vm_name
  vm_template_id          = var.vm_template_id
  vm_cores                = var.vm_cores
  vm_memory_mb            = var.vm_memory_mb
  vm_disk_gb              = var.vm_disk_gb
  vm_storage              = var.vm_storage
  vm_bridge               = var.vm_bridge
  vm_mac_address          = var.vm_mac_address
  ssh_authorized_keys     = var.ssh_authorized_keys
  user_data_template_path = "${path.root}/../../../../../proxmox/snippets/docker-host.user-data.yaml.tftpl"

  # OpenSSH retient la première valeur : passer avant 50-cloud-init.conf.
  ssh_config_path = "/etc/ssh/sshd_config.d/00-homelab.conf"

  vm_bios    = "ovmf"
  vm_machine = "q35"

  vm_efi_disk = {
    type              = "4m"
    pre_enrolled_keys = false
  }
}
