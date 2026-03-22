variable "pm_node" {
  type = string
}

variable "vm_name" {
  type = string
}

variable "vm_template_id" {
  type = number
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
  default = 40
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
  type      = list(string)
  sensitive = true

  validation {
    condition     = length(var.ssh_authorized_keys) > 0
    error_message = "ssh_authorized_keys must contain at least one SSH public key."
  }
}

variable "user_data_template_path" {
  type        = string
  description = "Path to the cloud-init user-data Terraform template (.tftpl)."
}
