Scope: this covers the findings from `pe_dns_audit.py`. The Azure Monitor alerts in your Terraform are Service Health alerts, which you can't trigger from a demo.

**Base layout** (one demo subscription, Terraform, random suffix, tear down after)

- vnet-a: Azure DNS, the "good" VNet
- vnet-b: custom DNS server set to a dummy IP, e.g. 10.0.0.4
- vnet-c: Azure DNS, not peered, not linked
- vnet-d: peered to vnet-a (negative test)
- Zone `privatelink.blob.core.windows.net`, linked to vnet-a only, auto-registration off

**Trigger matrix**

| ID | How to trigger |
|---|---|
| Healthy baseline | Storage account with a blob PE in vnet-a and a zone group on the blob zone. Public access off. |
| PE03 missing record | Same as the baseline, then delete the A record. A zone group avoids also firing PE05. |
| PE04 stale IP | Same as the baseline, then edit the A record to a wrong IP. |
| PE05 MEDIUM | Blob PE in vnet-a with no zone group. |
| PE05 LOW | Blob PE in vnet-b with no zone group. |
| NW01 | The vnet-b PE above also fires this. |
| PE09 wrong zone | Blob PE whose zone group points at `privatelink.file.core.windows.net`. |
| PE02 no zone | Key Vault PE with no `privatelink.vaultcore.azure.net` zone anywhere. |
| ZN05 not reachable | Blob PE in vnet-c with a zone group on the blob zone. The record and IP are correct, but vnet-c has no link and no peer. |
| PE01 pending (MEDIUM) | PE created with `--manual-request true` and left unapproved. |
| PE01 rejected (HIGH) | Create a PE, then reject the connection on the target. |
| PE08 deleted target | Create a PE, then delete the storage account. |
| TG01 MEDIUM | Storage with a PE, public access enabled, firewall default Allow. |
| TG01 LOW | Storage with a PE, public access enabled, firewall default Deny. |
| RC01 orphan | Add an A record in the blob zone pointing to 10.99.99.99. |
| ZN01 no links | Create `privatelink.queue.core.windows.net` and link it to nothing. |
| ZN02 duplicate | Create `privatelink.table.core.windows.net` in two resource groups. |
| ZN03 auto-registration | Throwaway privatelink zone with a VNet link where registration is on (`-e true`). |

**Cannot be triggered reliably, so use fixtures**

PE07 (failed provisioning), PE06 (no NIC/FQDN data), ZN04 (link not Completed) and AU01 (unreadable zone) are transient or permission-dependent states. Add a `--fixture file.json` mode that bypasses `collect()` and feeds canned data into `analyze()`. This also gives you fast regression tests that need no Azure.

**Negative tests (no finding expected)**

- A PE in vnet-d, with the zone linked to vnet-a only: must show "Peered network" and no ZN05.
- A custom-DNS PE with a zone group: must show LOW, not HIGH.
- A Key Vault PE with a zone group on `vaultcore`: tests the `.vault.azure.net` alias.

**Pass criteria**

Write an `expected.csv` of (resource, check ID, severity), then diff it against `findings.csv`. Fail on any missing or unexpected finding. Run it in CI against the demo env, and every time you edit the `EXPECTED` zone table.

**Trade-offs**

- Cost: about 10 PEs, roughly $7 per month each if left running. Create and destroy per run, or keep the VNets and zones and cycle only the PEs.
- A deleted-target PE can't be created in one apply. Script it as create, then delete, in the test harness.
- Deliberate misconfigurations cause cross-firing. The isolation notes above handle it, but verify with a first run.

I can generate the Terraform for the base layout and cases, the `expected.csv`, and the fixture mode patch. Which first?