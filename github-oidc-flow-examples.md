# GitHub Actions → Azure OIDC: setup, runtime flow and job templating

Example values are illustrative (`kid`, key material, token strings, `exp`, commit SHAs are made up). Field names are real.

Three parts:

- **Part A:** one-time setup (creating the federated identity)
- **Part B:** runtime flow (every pipeline run)
- **Part C:** job templating (reusable workflows) and what it does to `sub`

---

# Part A: One-time setup (creating the federated identity)

You do this once per identity and environment. It creates the trust rule that Part B relies on.

```
You (az / Terraform)        Entra ID + Azure                GitHub repo settings
   | 1 create identity ------> id-gh-apply (managed identity)
   | 2 add federated cred ---> issuer + subject + audience stored on the identity
   | 3 assign RBAC ----------> Contributor on rg-oidc-lab-target
   | 4 set variables + environments ------------------------> vars, env rules, branch rules
```

## A1. Create the identity

```bash
az identity create -n id-gh-apply -g rg-oidc-lab-ci -l uaenorth
az identity show -n id-gh-apply -g rg-oidc-lab-ci \
  --query "{clientId:clientId, principalId:principalId}" -o json
```

- `clientId` is what the pipeline logs in as (`AZURE_CLIENT_ID`).
- `principalId` (object ID) is what RBAC role assignments use.

## A2. Add the federated credential (the trust rule)

```bash
az identity federated-credential create \
  --name gh-env-dev \
  --identity-name id-gh-apply \
  --resource-group rg-oidc-lab-ci \
  --issuer https://token.actions.githubusercontent.com \ # <----This 
  --subject "repo:imranM0456/oidc-lab:environment:dev" \ # <----This 
  --audiences api://AzureADTokenExchange
```

Terraform equivalent:

```hcl
resource "azurerm_federated_identity_credential" "apply_dev" {
  name                = "gh-env-dev"
  resource_group_name = azurerm_resource_group.ci.name
  parent_id           = azurerm_user_assigned_identity.apply.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  subject             = "repo:imranM0456/oidc-lab:environment:dev"
}
```

- The three values must equal the token's `iss`, `sub` and `aud` exactly (Part B, step 5).
- It stores no secret and grants no permissions.
- One credential per `sub` you want to accept (e.g. `environment:dev`, `environment:prod`, `pull_request`).
- Create credentials one at a time on the same identity (parallel writes conflict).

## A3. Assign RBAC

```bash
az role assignment create \
  --assignee-object-id <principalId> --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope /subscriptions/<sub-id>/resourceGroups/rg-oidc-lab-target
```

- Without this, login succeeds but every call returns 403.
- Scope to the resource group, not the subscription.

## A4. GitHub side

- Repo variables (not secrets): `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `APPLY_CLIENT_ID`, `STATE_ACCOUNT`.
- Environment `dev`: deployment branches limited to `test/*`.
- Environment `prod`: required reviewer, branch `main` only.
- Branch protection on `main`.

## A5. Check it before running a pipeline

```bash
az identity federated-credential list \
  --identity-name id-gh-apply -g rg-oidc-lab-ci -o table
```

A new credential can take a minute or two to work.

---

# Part B: Runtime flow (every run)

```
GitHub job          GitHub OIDC          Entra ID            Azure ARM
    |  1 request token  |                    |                    |
    |------------------>|                    |                    |
    |  2 signed JWT     |                    |                    |
    |<- - - - - - - - - |                    |                    |
    |  3 JWT + client ID                     |                    |
    |--------------------------------------->|                    |
    |                   |  4 fetch JWKS      |                    | (JSON Web Key Set)
    |                   |<-------------------|                    |
    |                   |            [verify sig, iss, sub, aud, exp]
    |  5 Azure access token                  |                    |
    |<- - - - - - - - - - - - - - - - - - - -|                    |
    |  6 API call + token                                         |
    |------------------------------------------------------------>|
    |                                                  [RBAC check]
```

## Step 1: Job asks GitHub for a token

**GitHub job → GitHub OIDC**

```
GET $ACTIONS_ID_TOKEN_REQUEST_URL&audience=api://AzureADTokenExchange
Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN
```

- Only works if the job has `permissions: id-token: write`.
- The bearer value is a per-job credential that GitHub injects.

## Step 2: GitHub returns a signed JWT

**GitHub OIDC → GitHub job**

```
header:  {"alg":"RS256","typ":"JWT","kid":"abc123"}
payload: {
  "iss": "https://token.actions.githubusercontent.com",
  "sub": "repo:imranM0456/oidc-lab:environment:dev",
  "aud": "api://AzureADTokenExchange",
  "repository": "imranM0456/oidc-lab",
  "environment": "dev",
  "ref": "refs/heads/test/first",
  "job_workflow_ref": "imranM0456/oidc-lab/.github/workflows/tf.yml@refs/heads/test/first",
  "exp": 1794674700
}
signature: signed with GitHub's private key
```

- GitHub fills in every claim. The job only chose `aud`.

## Step 3: Job sends the JWT to Entra

**GitHub job → Entra ID**

```
POST https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token
client_id=<APPLY_CLIENT_ID>
scope=https://management.azure.com/.default
grant_type=client_credentials
client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion=<the JWT from step 2>
```

- OAuth2 client credentials, with the JWT in place of a client secret.

## Step 4: Entra fetches GitHub's public keys

**Entra ID → GitHub OIDC**

```
GET https://token.actions.githubusercontent.com/.well-known/openid-configuration
  -> {"issuer": "...", "jwks_uri": "https://.../.well-known/jwks"}

GET .../.well-known/jwks
  -> {"keys":[{"kid":"abc123","kty":"RSA","alg":"RS256","n":"0vx7...","e":"AQAB"}]}
```

- The `kid` in the JWT header picks the matching key.
- `n` and `e` are the public key (modulus and exponent).

## Step 5: Entra verifies the token

**Entra ID**

| Check | Rule | Result |
|---|---|---|
| signature | verify with key `kid=abc123` | pass |
| `iss` | equals credential issuer (A2) | pass |
| `sub` | `repo:imranM0456/oidc-lab:environment:dev` equals credential subject exactly | pass |
| `aud` | `api://AzureADTokenExchange` | pass |
| `exp` | not yet expired | pass |

If `sub` were `...:environment:prod` instead, the result is `AADSTS700213`.

- This is the federated credential from A2. The `sub` rule is the security boundary.

## Step 6: Entra returns an Azure access token

**Entra ID → GitHub job**

```
{"token_type":"Bearer","expires_in":3599,"access_token":"eyJ0eXAi..."}

decoded access token:
  "aud": "https://management.azure.com"
  "idtyp": "app"
  "appid": "<APPLY_CLIENT_ID>"
  "oid": "<identity object id>"      (no user, no scp)
```

- Compare with an `az login` token, which has `idtyp: user` and `scp: user_impersonation`.

## Step 7: Terraform calls Azure with the token

**GitHub job → Azure ARM**

```
PUT https://management.azure.com/subscriptions/<sub-id>/resourceGroups/
    rg-oidc-lab-target/providers/Microsoft.Storage/storageAccounts/stdemodev1a2b3c
Authorization: Bearer eyJ0eXAi...
```

- ARM checks the token signature, `aud` and expiry, then moves to authorization.

## Step 8: ARM checks RBAC (from A3)

**Azure ARM**

| Identity | Scope | Result |
|---|---|---|
| apply identity (Contributor) | `rg-oidc-lab-target` | create storage account: allowed |
| apply identity | `rg-other` (no role) | 403 AuthorizationFailed |
| plan identity (Reader only) | same create call | 403 AuthorizationFailed |

- Federation decides **who** this is. RBAC decides **what it can do**.

---

# Part C: Job templating (reusable workflows)

Put the deploy logic in one central workflow and call it from many repos or pipelines.

```
caller repo (oidc-lab)                      template repo (pipeline-templates)
  deploy.yml
    job: dev ---- uses: ...tf-deploy.yml@ref ---->  tf-deploy.yml  (on: workflow_call)
                  with: environment, client_id...      job: environment: ${{ inputs.environment }}
                                                       permissions: id-token: write
                                                       steps -> Part B, steps 1-8
```

## C1. The template (central repo)

`pipeline-templates/.github/workflows/tf-deploy.yml`

```yaml
name: tf-deploy
on:
  workflow_call:
    inputs:
      environment:       { type: string, required: true }
      working_directory: { type: string, default: "." }
      client_id:         { type: string, required: true }
      tenant_id:         { type: string, required: true }
      subscription_id:   { type: string, required: true }
      state_account:     { type: string, required: true }

jobs:
  apply:
    runs-on: ubuntu-latest
    environment: ${{ inputs.environment }}
    permissions:
      id-token: write
      contents: read
    env:
      ARM_USE_OIDC: "true"
      ARM_USE_AZUREAD: "true"
      ARM_CLIENT_ID: ${{ inputs.client_id }}
      ARM_TENANT_ID: ${{ inputs.tenant_id }}
      ARM_SUBSCRIPTION_ID: ${{ inputs.subscription_id }}
    defaults:
      run:
        working-directory: ${{ inputs.working_directory }}
    steps:
      - uses: actions/checkout@v4          # checks out the CALLER repo
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init -backend-config="storage_account_name=${{ inputs.state_account }}"
      - run: terraform plan -out=tfplan
      - run: terraform apply tfplan
```

## C2. The caller (your repo)

`oidc-lab/.github/workflows/deploy.yml`

```yaml
name: deploy
on:
  push:
    branches: [main, "test/**"]

permissions:
  contents: read

jobs:
  dev:
    if: startsWith(github.ref, 'refs/heads/test/')
    permissions:
      id-token: write          # caller must grant it; the template can't exceed it
      contents: read
    uses: imranM0456/pipeline-templates/.github/workflows/tf-deploy.yml@<commit-sha>
    with:
      environment: dev
      working_directory: target
      client_id: ${{ vars.APPLY_CLIENT_ID }}
      tenant_id: ${{ vars.AZURE_TENANT_ID }}
      subscription_id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
      state_account: ${{ vars.STATE_ACCOUNT }}

  prod:
    if: github.ref == 'refs/heads/main'
    permissions:
      id-token: write
      contents: read
    uses: imranM0456/pipeline-templates/.github/workflows/tf-deploy.yml@<commit-sha>
    with:
      environment: prod
      working_directory: target
      client_id: ${{ vars.APPLY_CLIENT_ID }}
      tenant_id: ${{ vars.AZURE_TENANT_ID }}
      subscription_id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
      state_account: ${{ vars.STATE_ACCOUNT }}
```

- Pin `uses:` to a commit SHA (or a protected tag), never a moving branch you don't control.
- If the template repo is private, enable Settings → Actions → Access so other repos can use it.

## C3. What templating does to the token

Using a reusable workflow does **not** change the default `sub`. It still describes the **caller**:

```
sub: repo:imranM0456/oidc-lab:environment:dev
job_workflow_ref: imranM0456/pipeline-templates/.github/workflows/tf-deploy.yml@<ref>
```

- Your A2 federated credential keeps working unchanged.
- The template's identity is in a separate claim, `job_workflow_ref`, which Entra does **not** check by default.
- Consequence: any workflow in the caller repo that declares `environment: dev` and `id-token: write` gets the same `sub`, whether or not it uses your template.

## C4. Forcing "only via the template" (optional hardening)

Customize the `sub` template so it includes `job_workflow_ref`:

```bash
gh api -X PUT repos/imranM0456/oidc-lab/actions/oidc/customization/sub \
  -f use_default=false \
  -f 'include_claim_keys[]=repo' \
  -f 'include_claim_keys[]=context' \
  -f 'include_claim_keys[]=job_workflow_ref'
```

The `sub` then looks roughly like:

```
repo:imranM0456/oidc-lab:environment:dev:job_workflow_ref:imranM0456/pipeline-templates/.github/workflows/tf-deploy.yml@<ref>
```

- Decode a real token first to get the exact string, then put that in the federated credential subject.
- The `@<ref>` part must match how you call the template. Pinning to a new SHA means updating the credential, so many teams call a protected tag or branch and protect that ref in the template repo.
- Changing the `sub` template breaks all existing credentials for that repo until you update them. Do it in a lab first.

## C5. Templating gotchas

- **`id-token: write`:** a called workflow can't get more permissions than the caller grants. Set it on the caller job and the template job.
- **Environment:** declared in the template job (`environment: ${{ inputs.environment }}`). The `sub` still becomes `...:environment:dev`, and the environment's branch rules and reviewers still apply.
- **Composite actions vs reusable workflows:** a composite action runs inside the caller's job (no separate job, no separate `job_workflow_ref` trust). A reusable workflow runs as its own job.
- **Checkout:** `actions/checkout` in the template checks out the caller repo, not the template repo.
- **Variables:** pass client/tenant/subscription IDs as inputs. They aren't secrets.
- **One identity per environment:** keep plan and apply identities separate, and don't let the template pick a more privileged identity than the caller intended.

---

# Where errors come from

| Error | Where | Cause |
|---|---|---|
| "Unable to get ACTIONS_ID_TOKEN_REQUEST_URL" | B1 / C5 | `id-token: write` missing on the caller job or the template job |
| `AADSTS700213` | B5 | `sub`, `iss` or `aud` doesn't match the federated credential |
| `AADSTS700213` after customizing `sub` | C4 | old credentials still use the default `sub` format |
| `AADSTS700213` after a template update | C4 | `job_workflow_ref` `@<ref>` changed, credential not updated |
| Works, then fails right after creating the credential | A2 / B5 | propagation delay, retry in a minute or two |
| 403 AuthorizationFailed | B8 / A3 | RBAC role missing or too narrow |
| Template not found / not allowed | C2 | private template repo access not enabled, or wrong path/ref |
