#!/usr/bin/env python3
"""
pe_dns_audit.py (v2) - read-only Azure Private Endpoint / Private DNS auditor.

Finds silently broken private endpoint DNS: missing or stale A records, missing
or wrong privatelink zones, unlinked zones, orphan records, unapproved or failed
endpoints, endpoints whose target was deleted, public access left on, and
custom-DNS VNets. Also produces a per-endpoint inventory with a health status.

Requirements
  - Python 3.8+ (standard library only)
  - Azure CLI, logged in:  az login
  - Resource Graph extension:  az extension add --name resource-graph
  - Reader on the subscriptions to scan (no write access is used or needed)

Usage
  python pe_dns_audit.py                          # all enabled subscriptions
  python pe_dns_audit.py --subscriptions <id> <id> --client "Acme" --out ./report

Output: report.html, findings.csv, endpoints.csv, findings.json in the output folder.

Limits
  - Config-based. It checks what Azure says should resolve, not what actually
    resolves from inside your network. On-prem forwarders and custom DNS
    servers need a live resolution test.
  - Zones or endpoints in subscriptions you did not scan can cause false
    "missing zone", "orphan record" or "deleted target" findings. Scan the hub
    subscription too.
  - The service-to-zone table is maintained by hand. Check it against
    Microsoft's current private endpoint DNS zone list before relying on it.
    Services whose zone name varies by region or partition are not checked.

Credit
  Check coverage was informed by the MIT-licensed Azure.Admin.Console module
  (Chendrayan Venkatesan, PowerShell Gallery). The implementation here is
  original; no code was copied.
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

AZ = shutil.which("az")
SEV = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
STATUS = ["Failed", "Warning", "Review", "Healthy"]
AZURE_DNS = "168.63.129.16"

Q_PE = ("resources | where type =~ 'microsoft.network/privateendpoints' "
        "| project id, name, subscriptionId, resourceGroup, location, properties")
Q_NIC = ("resources | where type =~ 'microsoft.network/networkinterfaces' "
         "| where isnotnull(properties.privateEndpoint) | project id, properties")
Q_ZONES = ("resources | where type =~ 'microsoft.network/privatednszones' "
           "| where name contains 'privatelink' | project id, name, subscriptionId, resourceGroup")
Q_VNET = ("resources | where type =~ 'microsoft.network/virtualnetworks' "
          "| project id, name, subscriptionId, resourceGroup, dns=properties.dhcpOptions.dnsServers, "
          "peerings=properties.virtualNetworkPeerings")
Q_TARGET = ("resources | where id in~ ({ids}) | project id, name, type, subscriptionId, resourceGroup, "
            "pna=tostring(properties.publicNetworkAccess), acl=tostring(properties.networkAcls.defaultAction)")

# (resource type, group id) -> zone names the service resolves in. Hand-maintained.
EXPECTED = {}


def _exp(rtype, groups, zones):
    for g in groups:
        EXPECTED[(rtype, g.lower())] = list(zones)


for _g, _s in [("blob", "blob"), ("blob_secondary", "blob"), ("table", "table"),
               ("table_secondary", "table"), ("queue", "queue"), ("queue_secondary", "queue"),
               ("file", "file"), ("file_secondary", "file"), ("web", "web"),
               ("web_secondary", "web"), ("dfs", "dfs"), ("dfs_secondary", "dfs")]:
    _exp("microsoft.storage/storageaccounts", [_g], ["privatelink.%s.core.windows.net" % _s])
_exp("microsoft.keyvault/vaults", ["vault"], ["privatelink.vaultcore.azure.net"])
_exp("microsoft.keyvault/managedhsms", ["managedhsm"], ["privatelink.managedhsm.azure.net"])
_exp("microsoft.sql/servers", ["sqlServer"], ["privatelink.database.windows.net"])
_exp("microsoft.synapse/workspaces", ["Sql", "SqlOnDemand"], ["privatelink.sql.azuresynapse.net"])
_exp("microsoft.synapse/workspaces", ["Dev"], ["privatelink.dev.azuresynapse.net"])
_exp("microsoft.documentdb/databaseaccounts", ["Sql"], ["privatelink.documents.azure.com"])
_exp("microsoft.documentdb/databaseaccounts", ["MongoDB"], ["privatelink.mongo.cosmos.azure.com"])
_exp("microsoft.documentdb/databaseaccounts", ["Cassandra"], ["privatelink.cassandra.cosmos.azure.com"])
_exp("microsoft.documentdb/databaseaccounts", ["Gremlin"], ["privatelink.gremlin.cosmos.azure.com"])
_exp("microsoft.documentdb/databaseaccounts", ["Table"], ["privatelink.table.cosmos.azure.com"])
_exp("microsoft.documentdb/databaseaccounts", ["Analytical"], ["privatelink.analytics.cosmos.azure.com"])
_exp("microsoft.servicebus/namespaces", ["namespace"], ["privatelink.servicebus.windows.net"])
_exp("microsoft.eventhub/namespaces", ["namespace"], ["privatelink.servicebus.windows.net"])
_exp("microsoft.relay/namespaces", ["namespace"], ["privatelink.servicebus.windows.net"])
_exp("microsoft.eventgrid/topics", ["topic"], ["privatelink.eventgrid.azure.net"])
_exp("microsoft.eventgrid/domains", ["domain"], ["privatelink.eventgrid.azure.net"])
_exp("microsoft.containerregistry/registries", ["registry"], ["privatelink.azurecr.io"])
_exp("microsoft.web/sites", ["sites"], ["privatelink.azurewebsites.net"])
_exp("microsoft.dbforpostgresql/flexibleservers", ["postgresqlServer"], ["privatelink.postgres.database.azure.com"])
_exp("microsoft.dbforpostgresql/servers", ["postgresqlServer"], ["privatelink.postgres.database.azure.com"])
_exp("microsoft.dbformysql/flexibleservers", ["mysqlServer"], ["privatelink.mysql.database.azure.com"])
_exp("microsoft.dbformysql/servers", ["mysqlServer"], ["privatelink.mysql.database.azure.com"])
_exp("microsoft.dbformariadb/servers", ["mariadbServer"], ["privatelink.mariadb.database.azure.com"])
_exp("microsoft.cognitiveservices/accounts", ["account"],
     ["privatelink.cognitiveservices.azure.com", "privatelink.openai.azure.com",
      "privatelink.services.ai.azure.com"])
_exp("microsoft.search/searchservices", ["searchService"], ["privatelink.search.windows.net"])
_exp("microsoft.appconfiguration/configurationstores", ["configurationStores"], ["privatelink.azconfig.io"])
_exp("microsoft.cache/redis", ["redisCache"], ["privatelink.redis.cache.windows.net"])
_exp("microsoft.cache/redisenterprise", ["redisEnterprise"], ["privatelink.redisenterprise.cache.azure.net"])
_exp("microsoft.machinelearningservices/workspaces", ["amlworkspace"],
     ["privatelink.api.azureml.ms", "privatelink.notebooks.azure.net"])
_exp("microsoft.datafactory/factories", ["dataFactory"], ["privatelink.datafactory.azure.net"])
_exp("microsoft.datafactory/factories", ["portal"], ["privatelink.adf.azure.com"])
_exp("microsoft.signalrservice/signalr", ["signalr"], ["privatelink.service.signalr.net"])
_exp("microsoft.signalrservice/webpubsub", ["webpubsub"], ["privatelink.webpubsub.azure.com"])
_exp("microsoft.devices/iothubs", ["iotHub"], ["privatelink.azure-devices.net"])
_exp("microsoft.apimanagement/service", ["Gateway"], ["privatelink.azure-api.net"])
_exp("microsoft.databricks/workspaces", ["databricks_ui_api"], ["privatelink.azuredatabricks.net"])
_exp("microsoft.automation/automationaccounts", ["Webhook", "DSCAndHybridWorker"],
     ["privatelink.azure-automation.net"])


class AzError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def az(args):
    r = subprocess.run([AZ] + args + ["-o", "json"], capture_output=True, text=True)
    if r.returncode:
        raise AzError((r.stderr or r.stdout).strip()[:400])
    return json.loads(r.stdout) if r.stdout.strip() else None


def graph(query, subs):
    rows, token = [], None
    while True:
        args = ["graph", "query", "-q", query, "--first", "1000"]
        if subs:
            args += ["--subscriptions"] + subs
        if token:
            args += ["--skip-token", token]
        res = az(args) or {}
        rows += res.get("data", [])
        token = res.get("skip_token")
        if not token:
            return rows


# ---------- helpers ----------

# Public name suffixes whose privatelink zone has a different base name.
FQDN_ALIASES = [(".vault.azure.net", ".vaultcore.azure.net")]


def norm_fqdn(f):
    f = (f or "").strip().lower().rstrip(".")
    f = f.replace(".privatelink.", ".", 1)
    for pub, base in FQDN_ALIASES:
        if f.endswith(pub):
            f = f[:-len(pub)] + base
    return f


def public_suffix(zone):
    z = zone.lower()
    if z.startswith("privatelink."):
        return z[len("privatelink."):]
    return z.replace(".privatelink.", ".", 1)


def short(rid):
    return (rid or "").rstrip("/").split("/")[-1]


def ctype(target):
    return "/".join((target or "").lower().split("/providers/")[-1].split("/")[:2])


def expected_zones(conn):
    out = []
    for g in conn["groups"]:
        out += EXPECTED.get((ctype(conn["target"]), g.lower()), [])
    return out


def pe_connections(pe):
    out = []
    p = pe.get("properties") or {}
    for key in ("privateLinkServiceConnections", "manualPrivateLinkServiceConnections"):
        for c in p.get(key) or []:
            cp = c.get("properties") or {}
            st = (cp.get("privateLinkServiceConnectionState") or {}).get("status") or "Unknown"
            out.append({"target": cp.get("privateLinkServiceId") or "",
                        "groups": cp.get("groupIds") or [], "status": st})
    return out


def pe_pairs(pe, nics):
    """Return [(fqdn, ip)] for an endpoint, from its NIC (fallback: customDnsConfigs)."""
    pairs = []
    p = pe.get("properties") or {}
    for n in p.get("networkInterfaces") or []:
        nic = nics.get((n.get("id") or "").lower())
        if not nic:
            continue
        for ic in (nic.get("properties") or {}).get("ipConfigurations") or []:
            cp = ic.get("properties") or {}
            ip = cp.get("privateIPAddress")
            for f in (cp.get("privateLinkConnectionProperties") or {}).get("fqdns") or []:
                pairs.append((norm_fqdn(f), ip))
    if not pairs:
        for c in p.get("customDnsConfigs") or []:
            for ip in c.get("ipAddresses") or []:
                pairs.append((norm_fqdn(c.get("fqdn")), ip))
    return [(f, i) for f, i in pairs if f and i]


def pe_vnet_id(pe):
    sid = ((pe.get("properties") or {}).get("subnet") or {}).get("id", "").lower()
    return sid.split("/subnets/")[0]


def vnet_info(d, vid):
    v = d["vnets"].get(vid) or {}
    peers = set()
    for p in v.get("peerings") or []:
        rid = (((p.get("properties") or {}).get("remoteVirtualNetwork") or {}).get("id") or "").lower()
        if rid:
            peers.add(rid)
    return {"name": v.get("name") or short(vid), "dns": v.get("dns") or [], "peers": peers}


# ---------- collection ----------

def fetch_zone(z):
    base = ["--subscription", z["subscriptionId"], "-g", z["resourceGroup"], "-z", z["name"]]
    recs = az(["network", "private-dns", "record-set", "a", "list"] + base) or []
    links = az(["network", "private-dns", "link", "vnet", "list"] + base) or []
    return recs, links


def safe_graph(d, label, query, subs):
    try:
        return graph(query, subs)
    except AzError as e:
        d["unread"].add(label)
        d["notices"].append("%s could not be read, so checks that depend on them are incomplete: %s"
                            % (label.capitalize(), str(e)[:200]))
        return []


def collect(subs, args):
    d = {"subs": subs, "zonegroups": {}, "targets": {}, "targets_read": False,
         "notices": [], "unread": set()}
    log("Querying Resource Graph...")
    d["pes"] = graph(Q_PE, subs)  # fatal if it fails: nothing to audit without it
    d["nics"] = {n["id"].lower(): n for n in safe_graph(d, "network interfaces", Q_NIC, subs)}
    d["vnets"] = {v["id"].lower(): v for v in safe_graph(d, "virtual networks", Q_VNET, subs)}
    zones = safe_graph(d, "private DNS zones", Q_ZONES, subs)
    log("%d private endpoints, %d privatelink zones" % (len(d["pes"]), len(zones)))

    def zone_job(z):
        try:
            recs, links = fetch_zone(z)
            z["records"] = {r["name"].lower(): [a.get("ipv4Address") for a in r.get("aRecords") or []]
                            for r in recs}
            z["links"] = [{"vnet": ((l.get("virtualNetwork") or {}).get("id") or "").lower(),
                           "name": l.get("name"), "reg": bool(l.get("registrationEnabled")),
                           "state": l.get("virtualNetworkLinkState") or l.get("provisioningState") or ""}
                          for l in links]
            z["error"] = None
        except AzError as e:
            z["records"], z["links"], z["error"] = {}, [], str(e)
        return z

    log("Reading zone records and links...")
    with ThreadPoolExecutor(args.workers) as ex:
        d["zones"] = list(ex.map(zone_job, zones))

    if not args.skip_zone_groups:
        log("Reading DNS zone groups...")

        def zg_job(pe):
            try:
                res = az(["network", "private-endpoint", "dns-zone-group", "list", "--endpoint-name",
                          pe["name"], "-g", pe["resourceGroup"], "--subscription", pe["subscriptionId"]]) or []
                return pe["id"].lower(), [c.get("privateDnsZoneId", "").lower() for g in res
                                          for c in g.get("privateDnsZoneConfigs") or []]
            except AzError:
                return pe["id"].lower(), None

        with ThreadPoolExecutor(args.workers) as ex:
            d["zonegroups"] = dict(ex.map(zg_job, d["pes"]))
        unread = sum(1 for v in d["zonegroups"].values() if v is None)
        if unread:
            d["notices"].append("DNS zone groups of %d endpoint(s) could not be read. Zone group, "
                                "wrong-zone and link checks are skipped for them." % unread)

    if not args.skip_targets:
        log("Checking target resources...")
        ids = sorted({c["target"] for pe in d["pes"] for c in pe_connections(pe) if c["target"]})
        ok = True
        for i in range(0, len(ids), 50):
            chunk = ",".join("'%s'" % t.replace("'", "") for t in ids[i:i + 50])
            try:
                for t in graph(Q_TARGET.format(ids=chunk), subs):
                    d["targets"][t["id"].lower()] = t
            except AzError as e:
                ok = False
                d["notices"].append("Target resources could not be read, so public-access and "
                                    "deleted-target checks are skipped: %s" % str(e)[:200])
                break
        d["targets_read"] = ok
    return d


# ---------- analysis ----------

def analyze(d):
    F = []
    pe_issues = defaultdict(list)

    def add(sev, check, res, detail, fix="", sub="", pe=None):
        F.append({"severity": sev, "check": check, "resource": res,
                  "subscription": sub, "detail": detail, "fix": fix})
        if pe:
            pe_issues[pe].append((sev, check))

    zones = d["zones"]
    readable = [z for z in zones if not z.get("error")]
    zone_by_id = {z["id"].lower(): z for z in readable}
    subs_lower = {s.lower() for s in d["subs"]}
    vnets_ok = "virtual networks" not in d.get("unread", set())
    for z in zones:
        if z.get("error"):
            add("INFO", "AU01", z["name"], "Could not read zone (needs Reader / Private DNS Zone "
                "read access): " + z["error"][:200], sub=z["subscriptionId"])

    # zone-level checks
    by_name = defaultdict(list)
    for z in readable:
        by_name[z["name"].lower()].append(z)
    for name, zs in by_name.items():
        if len(zs) > 1:
            where = "; ".join("%s/%s" % (z["subscriptionId"], z["resourceGroup"]) for z in zs)
            add("MEDIUM", "ZN02", name, "Zone exists %d times (%s). Different VNets can resolve "
                "different answers (split DNS)." % (len(zs), where),
                "Keep one zone per name, link every VNet to it, move records, delete duplicates.")
    for z in readable:
        sub = z["subscriptionId"]
        if not z["links"]:
            add("HIGH", "ZN01", z["name"], "Zone has no virtual network links, so nothing can "
                "resolve its records.",
                "az network private-dns link vnet create -g %s -z %s -n <link-name> -v <vnet-id> "
                "-e false --subscription %s" % (z["resourceGroup"], z["name"], sub), sub)
        for l in z["links"]:
            if l["reg"]:
                add("MEDIUM", "ZN03", "%s (link %s)" % (z["name"], l["name"]),
                    "Auto-registration is enabled on a privatelink zone. It should be off.",
                    "az network private-dns link vnet update -g %s -z %s -n %s -e false --subscription %s"
                    % (z["resourceGroup"], z["name"], l["name"], sub), sub)
            if l["state"] and l["state"].lower() not in ("completed", "succeeded"):
                add("LOW", "ZN04", "%s (link %s)" % (z["name"], l["name"]),
                    "Link state is '%s', not Completed." % l["state"], "", sub)

    all_ips = set()
    for n in d["nics"].values():
        for ic in (n.get("properties") or {}).get("ipConfigurations") or []:
            ip = (ic.get("properties") or {}).get("privateIPAddress")
            if ip:
                all_ips.add(ip)

    pe_names = {}
    target_pes = defaultdict(list)
    inv_rows = []
    seen_link_notes = set()

    for pe in d["pes"]:
        name, sub = pe["name"], pe["subscriptionId"]
        key = pe["id"].lower()
        pe_names[key] = name
        props = pe.get("properties") or {}
        conns = pe_connections(pe)
        vnet = pe_vnet_id(pe)
        vi = vnet_info(d, vnet)
        approved = [c for c in conns if c["status"].lower() == "approved"]
        pairs = pe_pairs(pe, d["nics"])
        zg = d["zonegroups"].get(key)
        dns_zone_names = []
        linked_state = "Not checked"

        if (props.get("provisioningState") or "") == "Failed":
            add("HIGH", "PE07", name, "Endpoint provisioning state is Failed. It may not route "
                "traffic, and later changes can fail.",
                "Check the deployment error, then delete and recreate the endpoint.", sub, key)

        for c in conns:
            tgt = c["target"].lower()
            tsub = tgt.split("/")[2] if tgt.startswith("/subscriptions/") else ""
            orphan = bool(d.get("targets_read") and tgt and tsub in subs_lower
                          and tgt not in d["targets"]
                          and ctype(tgt) != "microsoft.network/privatelinkservices")
            if orphan:
                add("HIGH", "PE08", name, "Target %s was not found in the scanned subscriptions. If "
                    "it was deleted, this endpoint (and any hand-made DNS records) is orphaned."
                    % short(c["target"]), "Confirm the target is gone, then delete the endpoint and "
                    "its DNS records.", sub, key)
            elif c["status"].lower() != "approved":
                sev = "HIGH" if c["status"].lower() in ("rejected", "disconnected") else "MEDIUM"
                add(sev, "PE01", name, "Connection to %s is %s." % (short(c["target"]), c["status"]),
                    "Approve it on the target (Networking > Private endpoint connections), or "
                    "delete the endpoint if unused.", sub, key)
            elif c["target"]:
                target_pes[tgt].append(key)

        if approved:
            # zone group presence and wrong zone
            if zg == []:
                if vi["dns"]:
                    add("LOW", "PE05", name, "No DNS zone group, and VNet %s uses custom DNS (%s). Its "
                        "name resolves privately only if those servers hold or forward a record for it."
                        % (vi["name"], ", ".join(vi["dns"])),
                        "Check resolution from inside the VNet. Prefer a zone group on a zone linked "
                        "where those DNS servers resolve.", sub, key)
                else:
                    add("MEDIUM", "PE05", name, "No DNS zone group, and the VNet uses Azure DNS. Unless "
                        "a record was added by hand, the name resolves to the public address. Hand-made "
                        "records go stale when the IP changes.",
                        "az network private-endpoint dns-zone-group create -g %s --endpoint-name %s -n "
                        "default --private-dns-zone <zone-id> --zone-name <zone> --subscription %s"
                        % (pe["resourceGroup"], name, sub), sub, key)
            elif zg:
                dns_zone_names = [short(z) for z in zg]
                exp = sorted({z.lower() for c in approved for z in expected_zones(c)})
                if exp and not any(n.lower() in exp for n in dns_zone_names):
                    add("MEDIUM", "PE09", name, "DNS zone group uses %s, but this service resolves in %s. "
                        "The record sits in a zone clients never query, so they get the public address."
                        % (", ".join(dns_zone_names), " or ".join(exp)),
                        "Replace the zone group with one for %s." % exp[0], sub, key)

            if not pairs:
                add("INFO", "PE06", name, "No FQDN/IP data found (NIC unreadable or not provisioned). "
                    "DNS record checks skipped for this endpoint.", sub=sub, pe=key)
            good_zones = {}
            for fqdn, ip in pairs:
                matches = []
                for z in readable:
                    suf = public_suffix(z["name"])
                    if fqdn.endswith("." + suf):
                        matches.append((z, fqdn[:-(len(suf) + 1)]))
                hint = next(iter(sorted({z for c in approved for z in expected_zones(c)})), "")
                if not matches:
                    add("HIGH", "PE02", name, "No private DNS zone found for %s%s. Clients will resolve "
                        "the public address. The zone may be in a subscription that was not scanned."
                        % (fqdn, " (expected %s)" % hint if hint else ""),
                        "Create the zone%s, link it to the VNets that need it, then add a DNS zone group."
                        % (" " + hint if hint else ""), sub, key)
                    continue
                found = [(z, rec, z["records"].get(rec)) for z, rec in matches if rec in z["records"]]
                if not found:
                    z0, rec0 = matches[0]
                    add("HIGH", "PE03", name, "No A record for %s in %s (expected %s)."
                        % (fqdn, ", ".join(sorted({z["name"] for z, _ in matches})), ip),
                        "az network private-dns record-set a add-record -g %s -z %s -n %s -a %s "
                        "--subscription %s" % (z0["resourceGroup"], z0["name"], rec0, ip,
                                               z0["subscriptionId"]), sub, key)
                    continue
                good = [(z, rec) for z, rec, ips in found if ip in (ips or [])]
                if not good:
                    seen = "; ".join("%s -> %s" % (z["name"], ", ".join(ips or [])) for z, _, ips in found)
                    z0, rec0, _ = found[0]
                    add("HIGH", "PE04", name, "A record for %s points to the wrong IP (%s). Endpoint "
                        "IP is %s. Likely stale after the endpoint was recreated." % (fqdn, seen, ip),
                        "Update record '%s' in %s to %s, or recreate the DNS zone group so Azure "
                        "manages it." % (rec0, z0["name"], ip), sub, key)
                    continue
                for z, rec in good:
                    good_zones[z["id"].lower()] = z

            # link check, peering and custom-DNS aware. Uses the zone group's zones when known.
            check_zones = [zone_by_id[z] for z in (zg or []) if z in zone_by_id] or list(good_zones.values())
            if check_zones:
                if not dns_zone_names:
                    dns_zone_names = sorted({z["name"] for z in check_zones})
                linked = {l["vnet"] for z in check_zones for l in z["links"]}
                if vnet in linked:
                    linked_state = "Yes"
                elif linked & vi["peers"]:
                    linked_state = "Peered network"
                elif vi["dns"]:
                    linked_state = "Custom DNS"
                elif vnets_ok:
                    linked_state = "No"
                    nk = (key, ",".join(sorted(z["name"].lower() for z in check_zones)))
                    if nk not in seen_link_notes:
                        seen_link_notes.add(nk)
                        add("HIGH", "ZN05", name, "Zone(s) %s are linked to neither VNet %s nor a "
                            "peered VNet. Clients there resolve the public address." %
                            (", ".join(sorted(z["name"] for z in check_zones)), vi["name"]),
                            "Link the zone to %s (or to the hub whose DNS it uses)." % vi["name"],
                            sub, key)

        first = conns[0] if conns else {"target": "", "groups": [], "status": ""}
        inv_rows.append({"key": key, "endpoint": name, "subscription": sub,
                         "resource_group": pe["resourceGroup"], "target": short(first["target"]),
                         "group_ids": ", ".join(first["groups"]), "connection": first["status"],
                         "ips": ", ".join(sorted({i for _, i in pairs})),
                         "fqdns": ", ".join(sorted({f for f, _ in pairs})),
                         "dns_zones": ", ".join(dns_zone_names), "zone_linked": linked_state,
                         "vnet": vi["name"], "custom_dns": ", ".join(vi["dns"])})

    # target public access
    for tid, keys in target_pes.items():
        t = d["targets"].get(tid)
        if t and (t.get("pna") or "").lower() == "enabled":
            low = (t.get("acl") or "").lower() == "deny"
            names = sorted({pe_names[k] for k in keys})
            add("LOW" if low else "MEDIUM", "TG01", t["name"],
                "Public network access is enabled%s although it has a private endpoint (%s)."
                % (" (firewall default is Deny)" if low else " with no default-deny firewall",
                   ", ".join(names)),
                "Disable public network access if no public clients are needed (service-specific "
                "setting).", t.get("subscriptionId", ""))
            for k in keys:
                pe_issues[k].append(("LOW" if low else "MEDIUM", "TG01"))

    # orphan records
    for z in readable:
        if "azmk8s.io" in z["name"].lower():
            continue
        for rec, ips in z["records"].items():
            stray = [i for i in ips if i and i not in all_ips]
            if rec != "@" and stray:
                add("LOW", "RC01", "%s.%s" % (rec, z["name"]),
                    "A record points to %s, which belongs to no scanned private endpoint. Stale, or "
                    "the endpoint is in an unscanned subscription." % ", ".join(stray),
                    "Confirm and delete if stale.", z["subscriptionId"])

    # custom DNS on VNets that host endpoints
    for vid in sorted({pe_vnet_id(pe) for pe in d["pes"]}):
        vi = vnet_info(d, vid)
        if vi["dns"]:
            note = "" if AZURE_DNS in vi["dns"] else (" Make sure those servers forward privatelink "
                                                        "queries to %s or an Azure DNS Private Resolver." % AZURE_DNS)
            add("INFO", "NW01", vi["name"], "VNet uses custom DNS servers (%s).%s"
                % (", ".join(vi["dns"]), note), "Verify resolution from inside the VNet.")

    F.sort(key=lambda f: (SEV[f["severity"]], f["check"], f["resource"]))

    inv = []
    for r in inv_rows:
        issues = pe_issues.get(r.pop("key"), [])
        rank = min([SEV[s] for s, _ in issues], default=3)
        status = STATUS[rank]
        if rank == 3 and any(c == "PE06" for _, c in issues):
            status = "Unchecked"
        r["status"] = status
        r["issues"] = ", ".join(sorted({c for _, c in issues}))
        inv.append(r)
    order = {"Failed": 0, "Warning": 1, "Review": 2, "Unchecked": 3, "Healthy": 4}
    inv.sort(key=lambda r: (order[r["status"]], r["endpoint"]))
    return F, inv


# ---------- output ----------

def write_outputs(out, F, inv, d, client):
    os.makedirs(out, exist_ok=True)
    counts = {s: sum(1 for f in F if f["severity"] == s) for s in SEV}
    cols = ["severity", "check", "resource", "subscription", "detail", "fix"]
    with open(os.path.join(out, "findings.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, cols)
        w.writeheader()
        w.writerows(F)
    icols = ["status", "endpoint", "subscription", "resource_group", "target", "group_ids", "connection",
             "ips", "fqdns", "dns_zones", "zone_linked", "vnet", "custom_dns", "issues"]
    with open(os.path.join(out, "endpoints.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, icols)
        w.writeheader()
        w.writerows(inv)
    with open(os.path.join(out, "findings.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": dt.datetime.now().isoformat(timespec="seconds"), "summary": counts,
                   "notices": d.get("notices", []), "findings": F, "endpoints": inv}, fh, indent=2)
    e = html.escape
    frows = "".join(
        "<tr><td><span class='b %s'>%s</span></td><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td></tr>"
        % (f["severity"], f["severity"], e(f["check"]), e(f["resource"]), e(f["detail"]), e(f["fix"]))
        for f in F)
    irows = "".join(
        "<tr><td><span class='b S%s'>%s</span></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
        "<td>%s</td><td>%s</td></tr>"
        % (r["status"], r["status"], e(r["endpoint"]), e(r["target"]), e(r["group_ids"]),
           e(r["connection"]), e(r["ips"]), e(r["dns_zones"]) + (" (%s)" % e(r["zone_linked"])
                                                                if r["dns_zones"] else ""), e(r["issues"]))
        for r in inv)
    cards = "".join("<div class='c'><b>%d</b>%s</div>" % (n, s) for s, n in counts.items())
    notices = ("<div class='n'><b>Read problems</b><ul>%s</ul></div>"
               % "".join("<li>%s</li>" % e(n) for n in d["notices"])) if d.get("notices") else ""
    st = defaultdict(int)
    for r in inv:
        st[r["status"]] += 1
    stline = ", ".join("%d %s" % (st[s], s.lower()) for s in ["Failed", "Warning", "Review", "Unchecked",
                                                             "Healthy"] if st[s])
    doc = """<!doctype html><html><head><meta charset="utf-8"><title>Private endpoint DNS audit</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{max-width:1200px;margin:2rem auto;padding:0 1rem}
.cards{display:flex;gap:.75rem;margin:1rem 0}.c{padding:.75rem 1.25rem;border-radius:8px;border:1px solid #8884}
.c b{display:block;font-size:1.6rem}.n{border:1px solid #d68910;border-radius:8px;padding:.5rem 1rem;margin:1rem 0}
.b{padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;color:#fff;background:var(--x,#7f8c8d)}
.HIGH,.SFailed{--x:#c0392b}.MEDIUM,.SWarning{--x:#d68910}.LOW,.SReview{--x:#2e86c1}.INFO,.SUnchecked{--x:#7f8c8d}.SHealthy{--x:#27ae60}
table{border-collapse:collapse;width:100%%;font-size:.85rem;margin-bottom:2rem}th,td{border-bottom:1px solid #8884;padding:6px 8px;text-align:left;vertical-align:top}
code{font-size:.75rem;word-break:break-all}</style></head><body>
<h1>Private endpoint &amp; private DNS audit</h1>
<p>%s Generated %s. Scope: %d subscription(s), %d private endpoint(s), %d privatelink zone(s). Read-only, config-based.</p>
%s<h2>Findings</h2><div class="cards">%s</div>
<table><tr><th>Severity</th><th>Check</th><th>Resource</th><th>Finding</th><th>Suggested fix</th></tr>%s</table>
<h2>Endpoints</h2><p>%s</p>
<table><tr><th>Status</th><th>Endpoint</th><th>Target</th><th>Group</th><th>State</th><th>IPs</th><th>DNS zones (linked)</th><th>Issues</th></tr>%s</table>
</body></html>""" % (e(client), dt.date.today().isoformat(), len(d["subs"]), len(d["pes"]),
                     len(d["zones"]), notices, cards, frows, e(stline), irows)
    with open(os.path.join(out, "report.html"), "w", encoding="utf-8") as fh:
        fh.write(doc)
    return counts


def main():
    ap = argparse.ArgumentParser(description="Read-only Azure private endpoint / private DNS auditor")
    ap.add_argument("--subscriptions", nargs="*", help="Subscription IDs (default: all enabled)")
    ap.add_argument("--out", help="Output folder (default: pe-dns-audit-<date>)")
    ap.add_argument("--client", default="", help="Client name shown in the report")
    ap.add_argument("--workers", type=int, default=6, help="Parallel az calls (default 6)")
    ap.add_argument("--skip-zone-groups", action="store_true",
                    help="Skip reading DNS zone groups (faster; zone group, wrong-zone checks are lost)")
    ap.add_argument("--skip-targets", "--skip-public-access", dest="skip_targets", action="store_true",
                    help="Skip target lookups (public-access and deleted-target checks are lost)")
    args = ap.parse_args()

    if not AZ:
        sys.exit("Azure CLI (az) not found. Install it, then run: az login")
    try:
        az(["account", "show"])
    except AzError:
        sys.exit("Not logged in. Run: az login")
    subs = args.subscriptions or az(["account", "list", "--query", "[?state=='Enabled'].id"]) or []
    if not subs:
        sys.exit("No enabled subscriptions found.")
    log("Scanning %d subscription(s)" % len(subs))
    try:
        d = collect(subs, args)
    except AzError as e:
        msg = str(e)
        if "extension" in msg.lower() or "graph" in msg.lower():
            sys.exit("Resource Graph problem: %s\nTry: az extension add --name resource-graph" % msg)
        raise
    F, inv = analyze(d)
    out = args.out or "pe-dns-audit-" + dt.date.today().strftime("%Y%m%d")
    counts = write_outputs(out, F, inv, d, args.client)
    for n in d["notices"]:
        log("NOTE: " + n)
    log("Done: %s  ->  %s" % (", ".join("%s %d" % (k, v) for k, v in counts.items()),
                              os.path.join(out, "report.html")))


if __name__ == "__main__":
    main()
