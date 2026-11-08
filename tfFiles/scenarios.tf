locals {
  # vnet: where the PE lives | zg: zone group zone (null = none) | manual: manual approval
  # pna/acl: target public network access and firewall default action
  storage = {
    hlth = { vnet = "a", zg = "blob", manual = false, pna = false, acl = "Deny" } # healthy baseline
    pe03 = { vnet = "a", zg = "blob", manual = false, pna = false, acl = "Deny" } # record deleted by run-test.sh
    pe04 = { vnet = "a", zg = "blob", manual = false, pna = false, acl = "Deny" } # record repointed by run-test.sh
    pe5m = { vnet = "a", zg = null, manual = false, pna = false, acl = "Deny" }   # PE05 MEDIUM
    pe5l = { vnet = "b", zg = null, manual = false, pna = false, acl = "Deny" }   # PE05 LOW + NW01
    pe09 = { vnet = "a", zg = "file", manual = false, pna = false, acl = "Deny" } # wrong zone
    zn05 = { vnet = "c", zg = "blob", manual = false, pna = false, acl = "Deny" } # zone not reachable
    peer = { vnet = "d", zg = "blob", manual = false, pna = false, acl = "Deny" } # negative: peered
    cdns = { vnet = "b", zg = "blob", manual = false, pna = false, acl = "Deny" } # negative: custom DNS
    pend = { vnet = "a", zg = null, manual = true, pna = false, acl = "Deny" }    # PE01 pending
    rej  = { vnet = "a", zg = null, manual = true, pna = false, acl = "Deny" }    # PE01 rejected by script
    gone = { vnet = "a", zg = null, manual = false, pna = false, acl = "Deny" }   # target deleted by script
    tg1m = { vnet = "a", zg = "blob", manual = false, pna = true, acl = "Allow" } # TG01 MEDIUM
    tg1l = { vnet = "a", zg = "blob", manual = false, pna = true, acl = "Deny" }  # TG01 LOW
  }

  # No zone group, so add the correct A record by hand. Isolates PE05 / PE09 from PE03.
  manual_records = toset(["pe5m", "pe5l", "pe09"])
}

resource "azurerm_storage_account" "s" {
  for_each                      = local.storage
  name                          = "st${var.prefix}${random_string.sfx.result}${each.key}"
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = var.location
  account_tier                  = "Standard"
  account_replication_type      = "LRS"
  min_tls_version               = "TLS1_2"
  public_network_access_enabled = each.value.pna

  dynamic "network_rules" {
    for_each = each.value.pna ? [1] : []
    content {
      default_action = each.value.acl
      bypass         = ["AzureServices"]
    }
  }
}

resource "azurerm_private_endpoint" "s" {
  for_each            = local.storage
  name                = "pe-${each.key}"
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  subnet_id           = azurerm_subnet.pe[each.value.vnet].id

  private_service_connection {
    name                           = "psc-${each.key}"
    private_connection_resource_id = azurerm_storage_account.s[each.key].id
    subresource_names              = ["blob"]
    is_manual_connection           = each.value.manual
    request_message                = each.value.manual ? "demo approval" : null
  }

  dynamic "private_dns_zone_group" {
    for_each = each.value.zg == null ? [] : [each.value.zg]
    content {
      name                 = "default"
      private_dns_zone_ids = [local.zone_ids[private_dns_zone_group.value]]
    }
  }
}

resource "azurerm_private_dns_a_record" "manual" {
  for_each            = local.manual_records
  name                = azurerm_storage_account.s[each.key].name
  zone_name           = azurerm_private_dns_zone.blob.name
  resource_group_name = azurerm_resource_group.rg.name
  ttl                 = 300
  records             = [azurerm_private_endpoint.s[each.key].private_service_connection[0].private_ip_address]
}

# RC01: record pointing at an IP that belongs to no endpoint
resource "azurerm_private_dns_a_record" "orphan" {
  name                = "orphan-rc01"
  zone_name           = azurerm_private_dns_zone.blob.name
  resource_group_name = azurerm_resource_group.rg.name
  ttl                 = 300
  records             = ["10.99.99.99"]
}

# PE02: Key Vault PE with no vaultcore zone anywhere (also fires PE05, by design)
resource "azurerm_key_vault" "kv" {
  name                          = "kv${var.prefix}${random_string.sfx.result}"
  location                      = var.location
  resource_group_name           = azurerm_resource_group.rg.name
  tenant_id                     = data.azurerm_client_config.me.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  public_network_access_enabled = false
  soft_delete_retention_days    = 7
  purge_protection_enabled      = false
}

resource "azurerm_private_endpoint" "kv" {
  name                = "pe-kv"
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  subnet_id           = azurerm_subnet.pe["a"].id

  private_service_connection {
    name                           = "psc-kv"
    private_connection_resource_id = azurerm_key_vault.kv.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
}
