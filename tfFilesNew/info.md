Run these in order after `apply` succeeds.

**1. Get names and IP**
```
terraform output pe_private_ip
SA=$(az storage account list -g rg-pe-demo --query "[0].name" -o tsv)
echo $SA
```
The IP should be 10.0.1.x.

**2. Control-plane checks**
```
az network private-endpoint show -g rg-pe-demo -n pe-blob-demo \
  --query "privateLinkServiceConnections[0].privateLinkServiceConnectionState.status" -o tsv
az network private-dns record-set a list -g rg-pe-demo -z privatelink.blob.core.windows.net -o table
az network private-dns link vnet list -g rg-pe-demo -z privatelink.blob.core.windows.net -o table
```
Expected results:
- PE status: `Approved`
- A record: `$SA` pointing to the same 10.0.1.x IP
- VNet link: `Completed`

**3. Resolution from inside the VNet (ACI)**
```
az container logs -g rg-pe-demo -n dnstest
```
Expected: the answer is `10.0.1.x`. If the log is empty, wait 30 to 60 seconds and retry. To re-run the test:
```
terraform apply -replace=azurerm_container_group.dnstest -var="location=uaenorth"
```

**4. Resolution from outside the VNet (WSL)**
```
nslookup $SA.blob.core.windows.net
```
Expected: a public IP, or a CNAME chain ending in a public IP. This shows split-horizon is working.

**5. Public access is blocked**
```
curl -sv https://$SA.blob.core.windows.net 2>&1 | grep -E "HTTP|403"
```
Expected: `403` (AuthorizationFailure, public network access disabled). That confirms only the PE path works.

**6. Break it on purpose (the useful part)**
- Remove the VNet link:
  ```
  az network private-dns link vnet delete -g rg-pe-demo -z privatelink.blob.core.windows.net -n link-vnet-pe-demo -y
  ```
  Then re-run the ACI test. It should now resolve to the public IP, which proves what the link does.
- Restore with `terraform apply`.

**7. Cleanup**
```
terraform destroy -var="location=uaenorth"
```