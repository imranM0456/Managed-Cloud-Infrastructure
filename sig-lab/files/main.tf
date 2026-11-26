# BOOTSTRAP: run locally with `az login`, local state.
# Creates: state storage, RGs, plan/apply identities, federated credentials, RBAC.

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
    random  = { source = "hashicorp/random", version = "~> 3.0" }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

variable "subscription_id" { type = string }
variable "github_owner" { default = "imranM0456" }
variable "github_repo" { default = "oidc-lab" }
variable "location" { default = "westeurope" }
variable "test_branch" { default = "test-deploy" } # used only by the branch-based credential

locals {
  issuer   = "https://token.actions.githubusercontent.com"
  audience = ["api://AzureADTokenExchange"]
  repo     = "${var.github_owner}/${var.github_repo}"
}

data "azurerm_client_config" "me" {}

# ---------- Resource groups ----------
resource "azurerm_resource_group" "ci" {
  name     = "rg-oidc-lab-ci"
  location = var.location
}

resource "azurerm_resource_group" "target" {
  name     = "rg-oidc-lab-target"
  location = var.location
}

# ---------- State storage (AAD auth only, versioned) ----------
resource "random_string" "sa" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_storage_account" "state" {
  name                            = "stoidclab${random_string.sa.result}"
  resource_group_name             = azurerm_resource_group.ci.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 7
    }
    container_delete_retention_policy {
      days = 7
    }
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.state.id
  container_access_type = "private"
}

# You need blob access too, to run terraform against this state from your laptop
resource "azurerm_role_assignment" "me_state" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.me.object_id
}

# ---------- Identities ----------
resource "azurerm_user_assigned_identity" "plan" {
  name                = "id-gh-plan"
  resource_group_name = azurerm_resource_group.ci.name
  location            = var.location
}

resource "azurerm_user_assigned_identity" "apply" {
  name                = "id-gh-apply"
  resource_group_name = azurerm_resource_group.ci.name
  location            = var.location
}

# ---------- Federated credentials (the trust rules: issuer + sub + aud) ----------
# PLAN identity: pull requests only
resource "azurerm_federated_identity_credential" "plan_pr" {
  name                = "gh-pr"
  resource_group_name = azurerm_resource_group.ci.name
  parent_id           = azurerm_user_assigned_identity.plan.id
  audience            = local.audience
  issuer              = local.issuer
  subject             = "repo:${local.repo}:pull_request"
}

# APPLY identity: environment dev (branch restriction lives in GitHub env rules)
resource "azurerm_federated_identity_credential" "apply_dev" {
  name                = "gh-env-dev"
  resource_group_name = azurerm_resource_group.ci.name
  parent_id           = azurerm_user_assigned_identity.apply.id
  audience            = local.audience
  issuer              = local.issuer
  subject             = "repo:${local.repo}:environment:dev"

  depends_on = [azurerm_federated_identity_credential.plan_pr]
}

# APPLY identity: environment prod
resource "azurerm_federated_identity_credential" "apply_prod" {
  name                = "gh-env-prod"
  resource_group_name = azurerm_resource_group.ci.name
  parent_id           = azurerm_user_assigned_identity.apply.id
  audience            = local.audience
  issuer              = local.issuer
  subject             = "repo:${local.repo}:environment:prod"

  depends_on = [azurerm_federated_identity_credential.apply_dev]
}

# APPLY identity: exact branch (only matches jobs with NO environment: set)
resource "azurerm_federated_identity_credential" "apply_branch" {
  name                = "gh-branch-test"
  resource_group_name = azurerm_resource_group.ci.name
  parent_id           = azurerm_user_assigned_identity.apply.id
  audience            = local.audience
  issuer              = local.issuer
  subject             = "repo:${local.repo}:ref:refs/heads/${var.test_branch}"

  depends_on = [azurerm_federated_identity_credential.apply_prod]
}

# ---------- RBAC ----------
resource "azurerm_role_assignment" "plan_reader" {
  scope                = azurerm_resource_group.target.id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.plan.principal_id
}

resource "azurerm_role_assignment" "apply_contrib" {
  scope                = azurerm_resource_group.target.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.apply.principal_id
}

# State access (plan needs write for the state lock)
resource "azurerm_role_assignment" "plan_state" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.plan.principal_id
}

resource "azurerm_role_assignment" "apply_state" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.apply.principal_id
}

# ---------- Outputs -> GitHub repo variables ----------
output "AZURE_TENANT_ID" { value = data.azurerm_client_config.me.tenant_id }
output "AZURE_SUBSCRIPTION_ID" { value = var.subscription_id }
output "PLAN_CLIENT_ID" { value = azurerm_user_assigned_identity.plan.client_id }
output "APPLY_CLIENT_ID" { value = azurerm_user_assigned_identity.apply.client_id }
output "STATE_ACCOUNT" { value = azurerm_storage_account.state.name }
