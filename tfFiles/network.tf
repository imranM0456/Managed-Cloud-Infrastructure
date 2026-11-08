resource "azurerm_resource_group" "rg" {
  name     = "${var.prefix}-rg"
  location = var.location
}

# Second RG only for the duplicate-zone case (ZN02)
resource "azurerm_resource_group" "dns2" {
  name     = "${var.prefix}-dns2-rg"
  location = var.location
}

locals {
  # a: good | b: custom DNS | c: unlinked, unpeered | d: peered to a
  vnet_prefix = { a = "10.10", b = "10.20", c = "10.30", d = "10.40" }
  custom_dns  = { b = ["10.20.0.4"] } # dummy server, never queried by the audit
}

resource "azurerm_virtual_network" "v" {
  for_each            = local.vnet_prefix
  name                = "${var.prefix}-vnet-${each.key}"
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  address_space       = ["${each.value}.0.0/16"]
  dns_servers         = lookup(local.custom_dns, each.key, null)
}

resource "azurerm_subnet" "pe" {
  for_each                          = local.vnet_prefix
  name                              = "pe"
  resource_group_name               = azurerm_resource_group.rg.name
  virtual_network_name              = azurerm_virtual_network.v[each.key].name
  address_prefixes                  = ["${each.value}.1.0/24"]
  private_endpoint_network_policies = "Disabled"
}

resource "azurerm_virtual_network_peering" "a_to_d" {
  name                      = "a-to-d"
  resource_group_name       = azurerm_resource_group.rg.name
  virtual_network_name      = azurerm_virtual_network.v["a"].name
  remote_virtual_network_id = azurerm_virtual_network.v["d"].id
}

resource "azurerm_virtual_network_peering" "d_to_a" {
  name                      = "d-to-a"
  resource_group_name       = azurerm_resource_group.rg.name
  virtual_network_name      = azurerm_virtual_network.v["d"].name
  remote_virtual_network_id = azurerm_virtual_network.v["a"].id
}

# ---------- zones ----------
resource "azurerm_private_dns_zone" "blob" {
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_private_dns_zone" "file" {
  name                = "privatelink.file.core.windows.net"
  resource_group_name = azurerm_resource_group.rg.name
}

# ZN01: deliberately never linked
resource "azurerm_private_dns_zone" "queue" {
  name                = "privatelink.queue.core.windows.net"
  resource_group_name = azurerm_resource_group.rg.name
}

# ZN02: same zone name in two RGs
resource "azurerm_private_dns_zone" "table1" {
  name                = "privatelink.table.core.windows.net"
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_private_dns_zone" "table2" {
  name                = "privatelink.table.core.windows.net"
  resource_group_name = azurerm_resource_group.dns2.name
}

# ZN03: auto-registration on a privatelink zone
resource "azurerm_private_dns_zone" "autoreg" {
  name                = "privatelink.autoreg.demo.test"
  resource_group_name = azurerm_resource_group.rg.name
}

locals {
  zone_ids = {
    blob = azurerm_private_dns_zone.blob.id
    file = azurerm_private_dns_zone.file.id
  }

  zone_links = {
    blob    = { zone = azurerm_private_dns_zone.blob.name, rg = azurerm_resource_group.rg.name, vnet = "a", reg = false }
    file    = { zone = azurerm_private_dns_zone.file.name, rg = azurerm_resource_group.rg.name, vnet = "a", reg = false }
    table1  = { zone = azurerm_private_dns_zone.table1.name, rg = azurerm_resource_group.rg.name, vnet = "a", reg = false }
    table2  = { zone = azurerm_private_dns_zone.table2.name, rg = azurerm_resource_group.dns2.name, vnet = "d", reg = false }
    autoreg = { zone = azurerm_private_dns_zone.autoreg.name, rg = azurerm_resource_group.rg.name, vnet = "b", reg = true }
  }
}

resource "azurerm_private_dns_zone_virtual_network_link" "l" {
  for_each              = local.zone_links
  name                  = "link-${each.key}-${each.value.vnet}"
  resource_group_name   = each.value.rg
  private_dns_zone_name = each.value.zone
  virtual_network_id    = azurerm_virtual_network.v[each.value.vnet].id
  registration_enabled  = each.value.reg
}
