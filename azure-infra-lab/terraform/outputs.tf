
output "resource_group_name" {
  description = "Name of the created resource group"
  value       = azurerm_resource_group.lab.name
}

output "resource_group_location" {
  description = "Azure region of the resource group"
  value       = azurerm_resource_group.lab.location
}

output "resource_group_id" {
  description = "Azure resource group ID"
  value       = azurerm_resource_group.lab.id
}
