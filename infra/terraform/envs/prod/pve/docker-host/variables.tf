variable "pm_endpoint" {
  type        = string
  description = "Ex: https://pve.home.arpa:8006"
}

variable "pm_api_token_id" {
  type        = string
  description = "Ex: terraform@pve!tf"
}

variable "pm_api_token_secret" {
  type      = string
  sensitive = true
}

variable "pm_tls_insecure" {
  type    = bool
  default = true
}

variable "pm_node" { type = string }

variable "vm_name" {
  type    = string
  default = "docker-prod-01"
}

variable "vm_template_id" {
  type    = number
  default = 9000
}

variable "vm_cores" {
  type    = number
  default = 4
}

variable "vm_memory_mb" {
  type    = number
  default = 8192
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

variable "ssh_authorized_keys" {
  type        = list(string)
  description = "SSH public keys injected into cloud-init for user remi."
  sensitive   = true

  validation {
    condition     = length(var.ssh_authorized_keys) > 0
    error_message = "ssh_authorized_keys must contain at least one SSH public key."
  }
}
