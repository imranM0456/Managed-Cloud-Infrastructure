
resource "azurerm_route_table" "app" {
  name                = "rt-app"
  location            = var.location
  resource_group_name = var.resource_group_name

  tags = {
    Environment = "Lab"
    ManagedBy   = "Terraform"
  }
}

resource "azurerm_subnet_route_table_association" "app" {
  subnet_id      = azurerm_subnet.app.id
  route_table_id = azurerm_route_table.app.id
}
