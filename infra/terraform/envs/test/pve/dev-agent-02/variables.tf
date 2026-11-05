variable "pm_endpoint" {
  type        = string
  description = "Ex: https://pve.reminat.com:8006"
}

variable "pm_api_token_id" {
  sensitive   = true
  type        = string
  description = "Ex: terraform@pve!tf"
}

variable "pm_api_token_secret" {
  type      = string
  sensitive = true
}

variable "pm_tls_insecure" {
  type    = bool
  default = false
}

variable "pm_node" { type = string }

variable "vm_name" {
  type    = string
  default = "dev-agent-02"
}

# Template Ubuntu 24.04 créé par infra/proxmox/scripts/template-ubuntu-2404.sh.
# Pas de valeur par défaut : VMID à vérifier dans Proxmox, puis à placer dans local.auto.tfvars.
variable "vm_template_id" {
  type        = number
  description = "VMID of the Ubuntu 24.04 UEFI template (not 9000)."

  validation {
    condition     = var.vm_template_id >= 100 && var.vm_template_id <= 999999999 && floor(var.vm_template_id) == var.vm_template_id && !contains([106, 9000, 9100], var.vm_template_id)
    error_message = "Set the VMID of the Ubuntu 24.04 template (>= 100). 106, 9000 and 9100 are reserved existing resources."
  }
}

variable "vm_cores" {
  type    = number
  default = 4
}

variable "vm_memory_mb" {
  type    = number
  default = 6144
}

variable "vm_disk_gb" {
  type    = number
  default = 80
}

variable "vm_storage" {
  type    = string
  default = "local-lvm"
}

variable "vm_bridge" {
  type    = string
  default = "vmbr0"
}

# MAC fixe pour la réservation DHCP : uniquement dans local.auto.tfvars (ignoré par Git).
# Jamais celle de dev-agent-01. null = MAC générée par Proxmox.
variable "vm_mac_address" {
  sensitive = true
  type      = string
  default   = null
}

variable "ssh_authorized_keys" {
  type        = list(string)
  description = "SSH public keys injected into cloud-init for user remi."
  sensitive   = true

  validation {
    condition     = length(var.ssh_authorized_keys) > 0
    error_message = "ssh_authorized_keys must contain at least one SSH public key."
  }
}
