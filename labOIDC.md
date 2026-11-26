**Manual = portal or `az` CLI.** Same three values: issuer, subject, audience.

## Portal: managed identity (matches the lab)

1. Azure portal → **Managed Identities** → select `id-gh-apply`.
2. Left menu → **Federated credentials** → **Add credential**.
3. Scenario: **GitHub Actions deploying Azure resources**.
4. Fill in:
   - Organization: `imranM0456`
   - Repository: `oidc-lab`
   - Entity type: **Environment** (or Branch / Pull request / Tag)
   - Environment name: `dev`
   - Name: `gh-env-dev`
5. Save. The portal builds the issuer, subject and audience for you. Check the generated subject reads `repo:imranM0456/oidc-lab:environment:dev`.

## Portal: app registration / SPN

1. **Microsoft Entra ID** → **App registrations** → your app.
2. **Certificates & secrets** → **Federated credentials** tab → **Add credential**.
3. Same scenario and fields as above.

## CLI: managed identity

```bash
az identity federated-credential create \
  --name gh-env-dev \
  --identity-name id-gh-apply \
  --resource-group rg-oidc-lab-ci \
  --issuer https://token.actions.githubusercontent.com \
  --subject "repo:imranM0456/oidc-lab:environment:dev" \
  --audiences api://AzureADTokenExchange

az identity federated-credential list \
  --identity-name id-gh-apply --resource-group rg-oidc-lab-ci -o table
```

## CLI: app registration

```bash
az ad app federated-credential create --id <app-object-id-or-appId> --parameters '{
  "name": "gh-env-dev",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:imranM0456/oidc-lab:environment:dev",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

## Manual full setup order (no Terraform)

1. Create the managed identity (`az identity create`).
2. Add the federated credential (above).
3. Assign RBAC: `az role assignment create --assignee <principalId> --role Contributor --scope <RG id>`.
4. In GitHub, set repo variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID`. They're not secrets.
5. Workflow uses `permissions: id-token: write` and the `azure/login` action with those three values.

## Gotchas

- The subject must match **exactly**, including case. The portal's generator avoids typos, so use it the first time.
- Entity type decides the subject form. If the job has `environment:`, use Environment. A Branch credential won't match it.
- A new credential can take a minute or two to work. Retry before debugging.
- Don't add several credentials to the same identity at once in scripts. Run them one after another.
- Manual changes aren't tracked. If you later manage the same identity in Terraform, import the credential or you'll get conflicts.