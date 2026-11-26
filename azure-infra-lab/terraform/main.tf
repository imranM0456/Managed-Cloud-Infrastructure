
resource "azurerm_resource_group" "lab" {
  name     = var.resource_group_name
  location = var.location

  tags = {
    Environment = "Lab"
    Project     = "Managed-Cloud-Infrastructure"
    ManagedBy   = "Terraform"
    Owner       = "Infrastructure-Team"
  }
}
