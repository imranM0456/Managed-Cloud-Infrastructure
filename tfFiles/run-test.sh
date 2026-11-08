#!/usr/bin/env bash
# Usage: SUB=<demo-subscription-id> ./run-test.sh
# Needs: terraform, az (logged in, resource-graph extension), jq, python3
# Teardown: terraform destroy -auto-approve
set -euo pipefail
cd "$(dirname "$0")"
: "${SUB:?set SUB to the demo subscription id}"
AUDIT="${AUDIT:-../pe_dns_audit.py}"
GRAPH_WAIT="${GRAPH_WAIT:-180}"   # Resource Graph is eventually consistent
export TF_VAR_subscription_id="$SUB"

terraform init -input=false
terraform apply -auto-approve -input=false

az account set -s "$SUB"
RG=$(terraform output -raw resource_group)
ZONE=privatelink.blob.core.windows.net
out() { terraform output -json "$1" | jq -r ".$2"; }

echo "Waiting for zone group records..."; sleep 45

echo "PE03: delete the record the zone group created"
az network private-dns record-set a delete -g "$RG" -z "$ZONE" -n "$(out storage_names pe03)" --yes

echo "PE04: repoint record at another endpoint's IP (avoids also firing RC01)"
N=$(out storage_names pe04)
az network private-dns record-set a delete -g "$RG" -z "$ZONE" -n "$N" --yes
az network private-dns record-set a add-record -g "$RG" -z "$ZONE" -n "$N" -a "$(out pe_ips hlth)"

echo "PE01 HIGH: reject the pending connection"
CID=$(az network private-endpoint-connection list --id "$(out storage_ids rej)" --query "[0].id" -o tsv)
az network private-endpoint-connection reject --id "$CID" --description "demo"

echo "PE08: delete the target, keep the endpoint"
az storage account delete -n "$(out storage_names gone)" -g "$RG" --yes

echo "Waiting ${GRAPH_WAIT}s for Resource Graph..."; sleep "$GRAPH_WAIT"
python3 "$AUDIT" --subscriptions "$SUB" --client demo --out ./audit-out

python3 - <<'PY'
import csv, re, sys
exp = list(csv.DictReader(open("expected.csv")))
act = list(csv.DictReader(open("audit-out/findings.csv")))
m = lambda e, a: (e["check"] == a["check"] and e["severity"] == a["severity"]
                  and re.fullmatch(e["resource"], a["resource"]))
missing = [e for e in exp if not any(m(e, a) for a in act)]
extra = [a for a in act if not any(m(e, a) for e in exp)]
for e in missing: print("MISSING   %(check)s %(severity)s %(resource)s" % e)
for a in extra:   print("UNEXPECTED %(check)s %(severity)s %(resource)s" % a)
print("PASS" if not (missing or extra) else "FAIL")
sys.exit(1 if (missing or extra) else 0)
PY
