# Throwaway DNS test client. Delete this file and apply to remove it.

resource "azurerm_subnet" "aci" {
  name                 = "snet-aci"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.0.2.0/24"]

  delegation {
    name = "aci"
    service_delegation {
      name    = "Microsoft.ContainerInstance/containerGroups"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }
  }
}

resource "azurerm_container_group" "dnstest" {
  name                = "dnstest"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  os_type             = "Linux"
  ip_address_type     = "Private"
  subnet_ids          = [azurerm_subnet.aci.id]
  restart_policy      = "Never"

container {
  name     = "nslookup"
  image    = "busybox"
  cpu      = "0.5"
  memory   = "0.5"
  commands = ["nslookup", azurerm_storage_account.sa.primary_blob_host]

  ports {
    port     = 80
    protocol = "TCP"
  }
}

  depends_on = [azurerm_private_endpoint.pe]
}
