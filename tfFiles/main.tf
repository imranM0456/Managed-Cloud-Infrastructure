terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.42.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  subscription_id                 = "a3496361-d913-432d-98bd-98bd6cae7ed4"
  resource_provider_registrations = "none"

  features {
    key_vault { 
      purge_soft_delete_on_destroy = true 
    }
    
    # Fix: Changed from 'storage_account' to 'storage'
    storage {
      data_plane_available = false
    }
  }
}


variable "subscription_id" {
  type        = string
  description = "DEDICATED demo subscription. The audit scans all of it, so other PEs pollute results."
}

variable "location" {
  type    = string
  default = "australiasoutheast"
}

variable "prefix" {
  type    = string
  default = "pedemo"
}

data "azurerm_client_config" "me" {}

resource "random_string" "sfx" {
  length  = 5
  special = false
  upper   = false
}

output "subscription_id" { value = var.subscription_id }
output "resource_group" { value = azurerm_resource_group.rg.name }
output "storage_names" { value = { for k, s in azurerm_storage_account.s : k => s.name } }
output "storage_ids" { value = { for k, s in azurerm_storage_account.s : k => s.id } }
output "pe_ips" {
  value = { for k, p in azurerm_private_endpoint.s : k => p.private_service_connection[0].private_ip_address }
}
