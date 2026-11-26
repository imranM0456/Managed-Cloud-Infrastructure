# Interview Q&A: GitHub → Azure OIDC / Workload Identity Federation

## Fundamentals

**1. How does GitHub Actions authenticate to Azure without a secret?**
The job requests a short-lived OIDC JWT from GitHub, signed with GitHub's private key. It sends that JWT to Entra as a client assertion. Entra verifies the signature using GitHub's public keys (JWKS), checks `iss`, `sub` and `aud` against the federated credential on the identity, and returns an Azure access token (~1h). Nothing secret is stored.

**2. What's a federated credential?**
A trust rule attached to an app registration or managed identity: issuer + subject + audience. It says "accept tokens that look exactly like this and let them act as this identity." It holds no secret and grants no permissions.

**3. Which claim is the security boundary, and why?**
`sub`. GitHub builds it from the real run context (repo, environment, branch, PR), so the job can't forge it. Entra matches it exactly, so a tight `sub` limits who can become the identity.

**4. Authentication vs authorization here?**
The federated credential answers who can become the identity (authentication). The RBAC role assignment answers what that identity can do (authorization).

**5. Why can't someone spoof the GitHub token?**
They'd need GitHub's private key to sign it. Entra gets the public keys over TLS from the issuer URL and verifies the signature. Replay is limited by short expiry and the `aud` check.

---

## Design

**6. Why separate plan and apply identities?**
Least privilege. Plan runs on PRs, which are less trusted, so it gets Reader only. Apply gets Contributor and is tied to protected environments. A compromised PR job can read but can't change anything.

**7. App registration vs user-assigned managed identity for this?**
Managed identity: no Graph permissions needed to create it, it lives in the resource group, and there's no secret surface at all. App registration: needed when the identity must hold Entra app permissions or work across tenants. For pipeline deploys, prefer a UAMI.

**8. How do you restrict deploys to specific branches?**
- **Environment-based:** the job declares `environment: dev`, the `sub` becomes `...:environment:dev`, and GitHub's environment rules restrict which branches can use it.
- **Branch-based:** an exact `ref:refs/heads/x` credential, which only matches jobs without an environment.
- Entra doesn't support wildcards in standard credentials, so the branch control usually lives in GitHub, plus branch protection.

**9. Where should RBAC be scoped?**
Resource group (or lower), never subscription-wide for a pipeline. Pair with separate identities per environment.

**10. How do you secure Terraform state?**
Storage account with Azure AD auth only (`shared_access_key_enabled = false`), RBAC for identities, versioning and soft delete, private endpoint with a self-hosted runner for real environments, and no secrets in state.

---

## Security / hardening

**11. What are the risks with OIDC federation?**
- Too-loose `sub` (a wildcard or org-level match)
- Anyone who can push to a matching branch or run a matching workflow
- Malicious PRs or third-party actions in a job that has `id-token: write`
- Repo rename or recreate, where someone re-registers the old name
- Trust in GitHub itself as the signer

**12. How do you mitigate them?**
- Exact `sub` per environment
- `id-token: write` only on jobs that need it
- Pin actions to commit SHAs
- Environment approvals for prod
- Branch protection
- Avoid `pull_request_target` with untrusted code
- Use immutable IDs (`repository_id`) in the `sub` template

**13. What happens with fork PRs?**
Fork PRs don't get `id-token`, so they can't authenticate to Azure. That's the default protection.

---

## Troubleshooting

**14. `AADSTS700213` or "no matching federated identity record". What do you check?**
Decode the token's `sub`, `iss` and `aud` and compare to the credential character by character. Common causes:
- Wrong entity type (job has an environment but the credential is branch-based)
- Case differences
- Wrong repo name
- Wrong identity's client ID

**15. Login works but Terraform plan returns 403?**
That's RBAC, not federation.
- Check the role assignment scope.
- For `azurerm`, a Reader-only identity fails on resource provider registration, so set `resource_provider_registrations = "none"`.
- For the backend, check blob data roles and `use_azuread_auth`.

**16. Works, then fails right after you create a credential?**
Propagation delay. Wait a minute or two and retry.

---

## Scenario questions

**17. "Migrate 50 pipelines from SPN secrets to OIDC."**
1. Inventory SPNs and secrets.
2. Create UAMIs or federated credentials per pipeline and environment.
3. Move RBAC to the new identities at the narrowest scope.
4. Switch workflows to `azure/login` with client/tenant/subscription IDs and OIDC.
5. Validate with parallel runs, then delete old secrets and disable old SPNs.
6. Automate credential creation in Terraform.

**18. "A pipeline in a different repo needs the same access."**
Add a separate federated credential with that repo's `sub`, or better, give it its own identity. Don't loosen the existing `sub` to match both.

**19. "Same flow for AKS pods?"**
Same pattern. The cluster's OIDC issuer signs the ServiceAccount token, and a federated credential on a UAMI trusts that issuer and `system:serviceaccount:<ns>:<sa>` as the subject. Pods get Azure tokens with no secrets.

---

## Concept check (if they probe deeper)

| Question | Answer |
|---|---|
| OAuth2 vs OIDC | OAuth2 = authorization (access token). OIDC = identity layer on top (ID token, discovery, JWKS). |
| What does the signature prove? | Authenticity (made by the key holder) and integrity (unchanged). Not secrecy, not truth of claims, not freshness. |
| Which flow is this? | OAuth2 client credentials with a federated JWT as the client assertion instead of a secret. |
| `scp` vs `roles` | `scp` = delegated permissions (acts as a user). `roles` = application permissions. |

---

## 30-second answer to memorize

> "CI gets a short-lived signed token from its identity provider. Entra verifies the signature against the issuer's published keys and checks that `iss`, `sub` and `aud` match the federated credential I configured, then issues an Azure token. There's no secret to leak. Security comes from an exact `sub`, separate plan and apply identities with least-privilege RBAC, and environment approvals on prod."

---

## Practice

Answer 1, 3, 6, 8 and 14 out loud without notes. Where you stall is what to revise.
