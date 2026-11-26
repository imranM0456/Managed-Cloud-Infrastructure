
variable "location" {
  description = "Azure region for the infrastructure lab"
  type        = string
  default     = "uaenorth"
}

variable "resource_group_name" {
  description = "Name of the infrastructure lab resource group"
  type        = string
  default     = "rg-azure-infra-lab"
}
