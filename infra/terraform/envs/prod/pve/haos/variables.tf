variable "pm_endpoint" {
  type        = string
  description = "Ex: https://pve.reminat.com:8006"
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

variable "vm_storage" {
  type    = string
  default = "local-lvm"
}

variable "vm_bridge" {
  type    = string
  default = "vmbr0"
}

variable "vm_name" {
  type    = string
  default = "haos-prod-01"
}

variable "vm_template_id" {
  type    = number
  default = 9100
}

variable "vm_cores" {
  type    = number
  default = 2
}

variable "vm_memory_mb" {
  type    = number
  default = 4096
}

variable "vm_disk_gb" {
  type    = number
  default = 64
}
