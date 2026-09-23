"""Pull SiteSense data for the RSGx PMO Dashboard and bake it into PMO_Dashboard.html.

Run this whenever you want fresh numbers:
    python fetch_data.py

Reads credentials from environment variables (SITESENSE_API_ENDPOINT,
SITESENSE_AUTH_HEADER) if set - the Railway deployment path - falling back to
config.json in this folder for local use, unchanged from before. Writes
PMO_Dashboard.html, a self-contained file you open directly in a browser. No
server needed for local use, and the API key never touches that file.

Data is emitted at MATERIAL level (not just project totals) so every dashboard
can filter and search down to an individual part. To keep the embedded payload
small, all repeated text is interned into a single `strings` table and rows are
stored as plain arrays of indices/numbers.
"""
import json
import os
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent

# Generated/runtime files (data.json, the built dashboard, the lock, the
# overlay JSON files) live here - HERE by default, unchanged for local use.
# Railway sets this to a mounted Volume path so they survive a redeploy;
# dashboard_template.html and fetch_data.py itself always come from HERE,
# the source checkout, never from DATA_DIR.
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# A manual refresh (the dashboard's "Refresh data" button, via refresh_server.py
# locally or server.py on Railway) can land at almost the same moment as the
# scheduled run. Two processes writing PMO_Dashboard.html at once would
# interleave and corrupt it, so the second one backs off instead. The lock is
# age-based rather than PID-based because reliably checking whether a PID is
# still alive is not portable on Windows; a run this size never legitimately
# takes over 20 minutes.
LOCK_FILE = DATA_DIR / ".fetch.lock"
LOCK_STALE_SECONDS = 20 * 60


def acquire_lock():
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            print(f"Another fetch_data.py run appears to be in progress "
                  f"(lock is {int(age)}s old). Exiting rather than risk "
                  f"interleaved writes to PMO_Dashboard.html. If that run has "
                  f"actually died, delete {LOCK_FILE.name} and try again.")
            raise SystemExit(0)
        print(f"Found a stale lock ({int(age)}s old) - a previous run likely "
              f"crashed without cleaning up. Taking it over.")
    LOCK_FILE.write_text(str(time.time()))


def release_lock():
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def _load_config():
    """Environment variables win (Railway); config.json is the local fallback.

    Never commit config.json - it holds the live SiteSense credentials. It is
    gitignored for exactly that reason.
    """
    env_url, env_auth = os.environ.get("SITESENSE_API_ENDPOINT"), os.environ.get("SITESENSE_AUTH_HEADER")
    if env_url and env_auth:
        return env_url, env_auth
    config_path = HERE / "config.json"
    if config_path.exists():
        cfg = json.loads(config_path.read_text())
        return cfg["api_endpoint"], cfg["auth_header_value"]
    raise SystemExit(
        "No SiteSense credentials found. Set SITESENSE_API_ENDPOINT and "
        "SITESENSE_AUTH_HEADER as environment variables, or create config.json "
        "locally (see config.json.example)."
    )


API_URL, AUTH_HEADER = _load_config()
PAGE_SIZE = 2500
MAX_PAGES_PER_TYPE = 60  # safety cap; the script logs loudly if it is ever hit

# Full RSGx project list pulled from https://admin.sitesensecloud.com/projects on 2026-08-06.
PROJECTS = [
    (3218, "ROMA"),
    (3219, "Procore: CBGU D&C JV - Roma St - AU-QLD-PMO-CBGUJV-EI-21-0179"),
    (4816, "Brisbane Move - AU-QLD-PMO-ACC-22-1068-MEI"),
    (11891, "1234 - Sandbox Test Project"),
    (14213, "Procore: NSJ BESS Construction - AU-WA-AP-MON-25-2781-PMO"),
    (14214, "Procore: Cloudbreak Solar Farm - AU-WA-PMO-FMG-25-2691"),
    (14215, "Procore: Western Ridge Crusher - AU-WA-AP-SED-24-2341-PMO"),
    (14216, "Procore: Sandbox Training Project v2 - 1111"),
    (14587, "Procore: Jimblebar East Project - AU-WA-AP-BHP-24-2073-PMO"),
    (15452, "Henderson Warehouse"),
    (15490, "Procore: Locomotive Charger - AU-WA-PMO-FMG-24-1946-MEI"),
    (15864, "Procore: DJARINDJIN & LOMBADINA WATER TREATMENT - AU-WA-PMO-BAL-24-2412"),
    (15865, "Procore: Flying Fish ANFO & Magazine Facility - AU-WA-PMO-FMG-25-2984"),
    (15866, "Procore: 3191 NSR KCGM 33kV Powerline D&C - AU-WA-PMO-NOR-25-3191"),
    (15867, "Procore: Cloudbreak Camp EPS Relocation and Upgrade - AU-WA-PMO-FMG-25-2646"),
    (15947, "Procore: AngloGold Ashanti's (AGA) Tropicana Gold Mine (TGM) - AU-WA-AP-ANG-25-2585-PMO"),
    (15948, "Procore: PTP Solomon and Queens Substations - AU-WA-PMO-FMG-25-2340"),
    (15949, "Procore: PTP Stage 2 Transmission Lines - AU-WA-PMO-FMG-25-2398"),
    (16622, "Procore: 2391 JV M&E Station Fit-out - AU-NSW-PMO-WBD-25-2391-MEI"),
    (16623, "Procore: Warrigal Substation - AU-WA-PMO-FMG-25-2374"),
    (16624, "Procore: CRY-KGN-KGS 132KV Substations Strike Energy - AU-WA-PMO-STR-25-3177"),
    (16625, "Procore: 33KV Powerline for Solar Farm - AU-WA-PMO-RSG-25-6688"),
    (16626, "Procore: PTP8 - Civil Foundations - AU-WA-PMO-FMG-25-3355"),
    (16627, "Procore: Greatland Gold Havieron Project - AU-WA-PMO-GRE-25-3155"),
    (16650, "Procore: PMO Reference Project - 1112"),
    (17287, "Procore: Electrical Services - Western Ridge Crusher Project - AU-WA-PS-SED-25-3453"),
    (17332, "Procore: 1870 SSTOM - Linewide MEP Final Fix - AU-NSW-PMO-WBD-25-1870-EI"),
    (17590, "Procore: PTP Stage 2 Substations - AU-WA-PMO-FMG-24-1820"),
    (17592, "Procore: Fortescue Decarbonisation - AU-WA-PMO-FMG-24-2388"),
    (17593, "Procore: PTP8 - Transmission Line - AU-WA-PMO-FMG-25-2500"),
    (17594, "Procore: BALDJA DJARINDJIN & LOMBADINA WATER TREATMENT - AU-WA-PMO-WC-24-2412"),
    (17648, "Procore: CHRISTMAS CREEK GREEN IRON PROJECT - AU-WA-PS-FMG-26-6661"),
    (18893, "Procore: Boddington Fault Works - AU-WA-AP-NEW-24-2006-PS"),
    (18894, "Procore: SASH Substation | Solomon Hub - AU-WA-PMO-FMG-26-3653-MEI"),
    (20437, "Procore: 2374.1 Warrigal - Reimbursable Scope - AU-WA-PMO-FMG-25-2374-MEI.1"),
]
PROJECT_IDS = [str(pid) for pid, _ in PROJECTS]
PROJECT_INDEX = {str(pid): i for i, (pid, _) in enumerate(PROJECTS)}

# --------------------------------------------------------------- region overlay
# SiteSense has no concept of an east/west region, so it is maintained here and
# baked into the dashboard. regions.json is the source of truth and is never
# overwritten: a refresh only adds projects that are not in it yet.
REGION_FILE = DATA_DIR / "regions.json"
WEST_CODES = ("AU-WA",)
EAST_CODES = ("AU-QLD", "AU-NSW", "AU-VIC", "AU-ACT", "AU-TAS")


def suggest_region(project_name):
    """Derive a starting region from the state code in the project name.

    Only the explicit AU-XX code is trusted. Anything without one is left blank
    for a human to set on the dashboard's Regions page rather than guessed at.
    """
    upper = project_name.upper()
    if any(c in upper for c in WEST_CODES):
        return "West"
    if any(c in upper for c in EAST_CODES):
        return "East"
    return ""


def load_regions():
    saved = {}
    if REGION_FILE.exists():
        try:
            saved = json.loads(REGION_FILE.read_text())
        except json.JSONDecodeError as exc:
            print(f"  !! regions.json is not valid JSON ({exc}). Leaving it alone "
                  f"and treating every project as unassigned for this run.")
            return {}
    added = 0
    for pid, name in PROJECTS:
        key = str(pid)
        if key not in saved:
            saved[key] = suggest_region(name)
            added += 1
    # Drop projects that no longer exist so the file does not grow stale.
    saved = {k: v for k, v in saved.items() if k in PROJECT_INDEX}
    REGION_FILE.write_text(json.dumps(saved, indent=2, sort_keys=True))
    assigned = sum(1 for v in saved.values() if v)
    print(f"  regions.json: {assigned}/{len(saved)} projects assigned"
          + (f", {added} newly suggested from project codes" if added else ""))
    return saved


# ------------------------------------------------------- delivery date overlay
# Confirmed on SiteSense's own PO Dashboard report: "Estimated Delivery Date" is
# a real, actively-used, per-line field at RSGx (values genuinely differ from
# the created date). It is NOT reachable through any field this GraphQL API
# exposes though - checked the full PurchaseOrder schema, lineProperties across
# 2000+ lines, resourceProperties, metadata/parents/attachments/sensors, and the
# NeedsDocument type, all for the exact PO line the UI displayed it on. So this
# is maintained as a local overlay instead, the same pattern as regions.json:
# entered once on the dashboard, never overwritten by a refresh.
DELIVERY_DATE_FILE = DATA_DIR / "delivery_dates.json"


def load_delivery_dates():
    if not DELIVERY_DATE_FILE.exists():
        return {}
    try:
        saved = json.loads(DELIVERY_DATE_FILE.read_text())
    except json.JSONDecodeError as exc:
        print(f"  !! delivery_dates.json is not valid JSON ({exc}). Leaving it "
              f"alone and treating every part as having no date entered this run.")
        return {}
    print(f"  delivery_dates.json: {len(saved)} dates on file")
    return saved


# Ledger document types, used for the three document-ledger tabs.
LEDGER_TYPES = [
    ("purchase_orders", "Purchase Orders", "allDataapiPurchaseOrders", "materials_inventory"),
    ("receipts", "Receipts", "allDataapiReceipts", "materials_inventory"),
    ("deliveries", "Deliveries", "allDataapiDeliveries", "materials_inventory"),
    ("inventory_audits", "Inventory Audits", "allDataapiInventoryAudits", "materials_inventory"),
    ("material_requests", "Material Requests", "allDataapiMaterialRequests", "track_trace"),
    ("transfer_notes", "Transfer Notes", "allDataapiTransferNotes", "track_trace"),
    ("transfer_receipts", "Transfer Receipts", "allDataapiTransferReceipts", "track_trace"),
    ("transfer_shipments", "Transfer Shipments", "allDataapiTransferShipments", "track_trace"),
    ("pick_tickets", "Pick Tickets", "allDataapiPickTickets", "track_trace"),
    ("shipments", "Shipments", "allDataapiShipments", "track_trace"),
    ("field_inspections", "Field Inspections", "allDataapiFieldInspections", "quality_compliance"),
    ("non_compliance_tickets", "Non-Compliance Tickets", "allDataapiNonComplianceTickets", "quality_compliance"),
]

HEADER_FIELDS = (
    "resourceGuid resourceName resourceStatusName resourceCreatedAt "
    "resourceUpdatedAt projectId"
)
LINE_FIELDS = (
    # resourceGuid identifies the DOCUMENT and is required so the ledger view can
    # dedupe correctly. Document numbers such as MR-00001 repeat across projects,
    # so deduping on name alone silently merges unrelated documents. resourceDescription
    # is here for the PO's own description (Receipt Dashboard's "PO Description").
    "resourceGuid resourceName resourceDescription resourceStatusName resourceCreatedAt "
    "resourceUpdatedAt projectId "
    "lineResourceGuid lineResourceName lineResourceUom lineResourceTypeName "
    "lineQuantity lineProperties lineFromReferenceDocumentName"
)
# Adds header-level resourceProperties, which is where the request/pick/issue
# chain carries its people fields (Requestor, Recipient Name, etc.) - see the
# Issuing Dashboard build below for exactly which keys.
LINE_FIELDS_WITH_PROPS = LINE_FIELDS + " resourceProperties"
RESOURCE_FIELDS = (
    "projectId resourceGuid resourceName resourceDescription resourceUom resourceTypeName "
    "resourceProperties"
)

# ------------------------------------------------------------ free issue material
# FIM is identified by its work package naming convention: the work package starts
# with "FIM". Nothing else is a reliable marker. Absence of a Procore originating
# system was tried and is far too broad, because vendors, labour and cable reels
# are all loaded by hand too, which pulled in roughly 2,000 records instead of the
# ~136 that are genuinely free issue.
FIM_WP_PREFIX = "FIM"
LOCATION_FIELDS = (
    "projectId resourceGuid locationName locationStatus locationCount containerGuid"
)
COUNTER_FIELDS = (
    "projectId resourceGuid countOnHand countAvailable countRequest countOrder "
    "countReceive countIssue countShip countHold countDamage countScrap countReturn"
)
# Receipts carry the detail the Receipt Dashboard needs: who checked it, where it
# was put away, the line number and the received date.
RECEIPT_FIELDS = LINE_FIELDS + (
    " lineNumber lineExecutionDate lineMetadataUpdatedByUserName"
    " lineFromReferenceDocumentTypeName"
)


# ---------------------------------------------------------------- API plumbing
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


def graphql(query):
    """POST a GraphQL query, retrying the gateway errors this API throws sporadically.

    The gateway returns the odd 502 under load. Without a retry a single blip
    kills a ten minute run, which also means the scheduled refresh silently
    fails, so transient HTTP and network errors back off and try again.
    """
    body = json.dumps({"query": query}).encode("utf-8")
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            API_URL,
            data=body,
            headers={"auth": AUTH_HEADER, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read())
            break
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                raise
        # OSError covers URLError, socket timeouts and ConnectionResetError, which
        # the gateway throws when it drops a large response mid-flight.
        except (OSError, json.JSONDecodeError) as exc:
            last = exc
            if attempt == MAX_ATTEMPTS:
                raise
        wait = min(30, 2 ** attempt)
        print(f"    retry {attempt}/{MAX_ATTEMPTS - 1} after {type(last).__name__}"
              f"{getattr(last, 'code', '')}, waiting {wait}s")
        time.sleep(wait)

    status = payload.get("status", {})
    if status.get("error_count", 0) or "errors" in payload.get("payload", {}):
        raise RuntimeError(f"SiteSense API error: {payload}")
    return payload["payload"]["data"]


def fetch_all_rows(list_field, label, fields, page_size=PAGE_SIZE):
    """Page through a list field across every project, returning raw edge nodes."""
    ids_literal = ",".join(f'"{pid}"' for pid in PROJECT_IDS)
    rows, after, page = [], None, 0
    while True:
        after_clause = f', after: "{after}"' if after else ""
        query = (
            f"{{ {list_field}(first: {page_size}, "
            f"filter: {{projectId: {{in: [{ids_literal}]}}}}{after_clause}) "
            f"{{ totalCount pageInfo {{ hasNextPage endCursor }} "
            f"edges {{ node {{ {fields} }} }} }} }}"
        )
        conn = graphql(query)[list_field]
        rows.extend(e["node"] for e in conn["edges"])
        page += 1
        print(f"  {label}: page {page}, {len(rows)}/{conn['totalCount']} rows")
        if not conn["pageInfo"]["hasNextPage"]:
            break
        if page >= MAX_PAGES_PER_TYPE * (PAGE_SIZE // max(1, page_size)):
            print(f"  !! {label}: HIT PAGE CAP. {conn['totalCount']} rows exist, "
                  f"only {len(rows)} fetched. Raise MAX_PAGES_PER_TYPE.")
            break
        after = conn["pageInfo"]["endCursor"]
    return rows


# ------------------------------------------------------------ string interning
class Strings:
    def __init__(self):
        self.list = []
        self.index = {}

    def idx(self, value):
        if value is None or value == "":
            value = ""
        value = str(value)
        got = self.index.get(value)
        if got is None:
            got = len(self.list)
            self.index[value] = got
            self.list.append(value)
        return got


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return int(f) if f == int(f) else round(f, 3)


def main():
    acquire_lock()
    try:
        _main()
    finally:
        release_lock()


def _main():
    print("Checking SiteSense API status...")
    status = json.loads(urllib.request.urlopen(urllib.request.Request(
        API_URL.rsplit("/query", 1)[0] + "/status", headers={"auth": AUTH_HEADER}
    ), timeout=30).read())
    print(f"  API status: {status}")
    if not status.get("up"):
        raise SystemExit("SiteSense API reports itself down. Aborting.")

    S = Strings()
    S.idx("")  # index 0 is the empty string

    # -------------------------------------------------- material master (by guid)
    # Smaller pages here: resourceProperties makes each row much heavier, and the
    # gateway drops the connection on very large responses.
    print("Fetching Resources (material master)...")
    resource_rows = fetch_all_rows("allDataapiResources", "Resources", RESOURCE_FIELDS,
                                   page_size=1000)

    materials = []                 # [nameIdx, descIdx, uomIdx, typeIdx, guidIdx]
    material_index_by_guid = {}    # resourceGuid -> position in `materials`

    def material_idx(guid, name=None, uom=None, type_name=None, desc=None):
        """Return the index for a material, creating/enriching the entry as needed."""
        if guid is None:
            guid = f"__name__{name}"
        pos = material_index_by_guid.get(guid)
        if pos is None:
            pos = len(materials)
            material_index_by_guid[guid] = pos
            # guidIdx lets the browser key a local overlay (delivery dates) to a
            # stable identity that survives across refreshes, unlike the array
            # position, which is only stable within one generated file.
            materials.append([S.idx(name or "(unnamed)"), S.idx(desc or ""),
                              S.idx(uom or ""), S.idx(type_name or ""), S.idx(guid)])
        else:
            entry = materials[pos]
            if entry[0] == S.idx("(unnamed)") and name:
                entry[0] = S.idx(name)
            if entry[1] == 0 and desc:
                entry[1] = S.idx(desc)
            if entry[2] == 0 and uom:
                entry[2] = S.idx(uom)
            if entry[3] == 0 and type_name:
                entry[3] = S.idx(type_name)
        return pos

    resource_meta = {}   # guid -> {name, type, origin, work package, project}
    for r in resource_rows:
        material_idx(r["resourceGuid"], r.get("resourceName"), r.get("resourceUom"),
                     r.get("resourceTypeName"), r.get("resourceDescription"))
        kv = {}
        for prop in (r.get("resourceProperties") or []):
            if isinstance(prop, dict) and prop.get("Key"):
                kv[prop["Key"]] = prop.get("Value")
        resource_meta[r["resourceGuid"]] = {
            "name": r.get("resourceName") or "",
            "type": r.get("resourceTypeName") or "",
            "origin": kv.get("Originating System") or "",
            "wp": kv.get("Work Package") or kv.get("Work Pack") or "",
            "pid": str(r["projectId"]),
        }
    print(f"  {len(materials)} distinct materials registered")

    def is_fim_part(guid):
        m = resource_meta.get(guid)
        return bool(m) and m["wp"].strip().upper().startswith(FIM_WP_PREFIX)

    fim_guids = {g for g in resource_meta if is_fim_part(g)}
    fim_packages = sorted({resource_meta[g]["wp"].strip() for g in fim_guids})
    print(f"  {len(fim_guids)} free issue material parts across "
          f"{len(fim_packages)} FIM work packages: {', '.join(fim_packages)}")

    # ------------------------------------------------------- line-level documents
    def fetch_lines(list_field, label):
        return fetch_all_rows(list_field, label, LINE_FIELDS)

    print("Fetching Purchase Order lines...")
    po_lines = fetch_lines("allDataapiPurchaseOrders", "Purchase Orders")
    print("Fetching Receipt lines...")
    receipt_lines = fetch_all_rows("allDataapiReceipts", "Receipts", RECEIPT_FIELDS)
    print("Fetching Shipment lines (used to resolve each receipt back to its PO)...")
    shipment_lines = fetch_lines("allDataapiShipments", "Shipments")
    # Issue Ticket / Material Request / Pick Ticket all need resourceProperties for
    # the Issuing Dashboard - that is where Requestor, Recipient Name and Handover
    # Date live (confirmed by inspecting real records via the SiteSense API).
    print("Fetching Issue Ticket lines (issued for installation and Issuing Dashboard)...")
    issue_lines = fetch_all_rows("allDataapiIssueTickets", "Issue Tickets", LINE_FIELDS_WITH_PROPS)
    print("Fetching Return Notice lines (returned to vendor)...")
    return_lines = fetch_lines("allDataapiReturnNotices", "Return Notices")
    print("Fetching Transfer Note lines...")
    transfer_lines = fetch_lines("allDataapiTransferNotes", "Transfer Notes")
    print("Fetching Material Request lines (requested quantity, for Issuing Dashboard)...")
    material_request_lines = fetch_all_rows("allDataapiMaterialRequests", "Material Requests",
                                             LINE_FIELDS_WITH_PROPS)
    print("Fetching Pick Ticket lines (picked quantity and pick location, for Issuing Dashboard)...")
    pick_ticket_lines = fetch_all_rows("allDataapiPickTickets", "Pick Tickets",
                                        LINE_FIELDS_WITH_PROPS)

    def line_material(r):
        return material_idx(
            r.get("lineResourceGuid"),
            r.get("lineResourceName"),
            r.get("lineResourceUom"),
            r.get("lineResourceTypeName"),
        )

    def props(r):
        p = r.get("lineProperties")
        return p if isinstance(p, dict) else {}

    # --------------------------------------------------- asset tracking (by part)
    # Joined on (project, material guid) which is stable across document types.
    # NOTE: receipts link to their shipment via lineFromReferenceDocumentName, not
    # to the PO, so the material guid is the only reliable join key here.
    print("Building Asset Tracking (ordered / received / issued / returned by part)...")
    assets = defaultdict(lambda: {
        "ordered": 0.0, "received": 0.0, "issued": 0.0, "returned": 0.0,
        "cost_code": "", "item_type": "", "unit_cost": 0.0,
        "pos": set(), "receipts": set(), "po_descs": set(), "po_latest": "",
    })

    for r in po_lines:
        key = (PROJECT_INDEX[str(r["projectId"])], line_material(r))
        a = assets[key]
        a["ordered"] += float(r.get("lineQuantity") or 0)
        p = props(r)
        a["cost_code"] = a["cost_code"] or (p.get("Cost Code") or "")
        a["item_type"] = a["item_type"] or (p.get("Line Item Type") or "")
        if not a["unit_cost"]:
            try:
                a["unit_cost"] = float(p.get("Unit Cost") or 0)
            except (TypeError, ValueError):
                pass
        if r.get("resourceName"):
            a["pos"].add(r["resourceName"])
        if r.get("resourceDescription"):
            a["po_descs"].add(r["resourceDescription"])
        # SiteSense's own Orders page shows Estimated Delivery Date equal to the
        # PO's created date on every PO checked (it is not a separate stored
        # field this API exposes), so the most recent PO's timestamp stands in
        # for it. Kept as the full timestamp: the browser converts it to the
        # local calendar date the same way SiteSense's page does.
        created = r.get("resourceCreatedAt") or ""
        if created > a["po_latest"]:
            a["po_latest"] = created

    for r in receipt_lines:
        key = (PROJECT_INDEX[str(r["projectId"])], line_material(r))
        a = assets[key]
        a["received"] += float(r.get("lineQuantity") or 0)
        p = props(r)
        a["cost_code"] = a["cost_code"] or (p.get("Cost Code") or "")
        a["item_type"] = a["item_type"] or (p.get("Line Item Type") or "")
        if r.get("resourceName"):
            a["receipts"].add(r["resourceName"])

    for r in issue_lines:
        key = (PROJECT_INDEX[str(r["projectId"])], line_material(r))
        assets[key]["issued"] += float(r.get("lineQuantity") or 0)

    for r in return_lines:
        key = (PROJECT_INDEX[str(r["projectId"])], line_material(r))
        assets[key]["returned"] += float(r.get("lineQuantity") or 0)

    def joined(names, cap=4):
        names = sorted(names)
        if not names:
            return ""
        if len(names) <= cap:
            return ", ".join(names)
        return ", ".join(names[:cap]) + f" +{len(names)-cap} more"

    asset_rows = []
    for (p_idx, m_idx), a in assets.items():
        asset_rows.append([
            p_idx, m_idx,
            S.idx(a["cost_code"]), S.idx(a["item_type"]),
            num(a["ordered"]), num(a["received"]), num(a["issued"]), num(a["returned"]),
            num(a["unit_cost"]),
            S.idx(joined(a["pos"])), S.idx(joined(a["receipts"])),
        ])
    print(f"  {len(asset_rows)} project/part combinations")

    # ------------------------------------------------------------------- receipts
    # A receipt line points at its shipment, and the shipment points at the PO, so
    # the PO column is resolved through that chain rather than guessed from the
    # receipt name. This is the same relationship SiteSense's own report walks.
    print("Building Receipt detail (walking receipt to shipment to PO)...")
    ship_to_po = {}
    for r in shipment_lines:
        po = r.get("lineFromReferenceDocumentName")
        if po and r.get("resourceName"):
            ship_to_po.setdefault((str(r["projectId"]), r["resourceName"]), po)

    # PO Description and PO Date for the Receipt Dashboard, keyed by (project, PO
    # number) since a receipt only carries the PO's name, not its own description.
    po_meta = {}
    for r in po_lines:
        key = (str(r["projectId"]), r.get("resourceName") or "")
        if key not in po_meta:
            po_meta[key] = (r.get("resourceDescription") or "", (r.get("resourceCreatedAt") or "")[:10])

    receipt_rows = []
    linked = 0
    for r in receipt_lines:
        pid = str(r["projectId"])
        if pid not in PROJECT_INDEX:
            continue
        p = props(r)
        ref = r.get("lineFromReferenceDocumentName") or ""
        ref_type = r.get("lineFromReferenceDocumentTypeName") or ""
        shipment = ref if ref_type == "Shipment" else ""
        po = ref if ref_type == "PO" else ship_to_po.get((pid, shipment), "")
        if po:
            linked += 1
        po_desc, po_date = po_meta.get((pid, po), ("", ""))
        received = (r.get("lineExecutionDate") or r.get("resourceCreatedAt") or "")
        receipt_rows.append([
            PROJECT_INDEX[pid],
            line_material(r),
            S.idx(r.get("resourceName") or ""),
            S.idx(r.get("resourceStatusName") or ""),
            S.idx((p.get("ReceiptStatus") or "").replace("SiteSense.Condition.", "")
                                               .replace("SiteSense.Status.", "")),
            S.idx(p.get("ReceiptLocation") or ""),
            S.idx(p.get("ReceiptCheckedBy") or r.get("lineMetadataUpdatedByUserName") or ""),
            S.idx(shipment),
            S.idx(po),
            S.idx(p.get("Cost Code") or ""),
            S.idx(p.get("Line Item Type") or ""),
            num(r.get("lineQuantity")),
            num(p.get("Unit Cost")),
            S.idx(received[:10]),
            S.idx(received[:7]),
            S.idx(r.get("lineNumber") or ""),
            S.idx(po_desc),
            S.idx(po_date),
        ])
    print(f"  {len(receipt_rows)} receipt lines, {linked} resolved to a purchase order")

    # ------------------------------------------------------------------ inventory
    print("Fetching Inventory Counters (stock position by material)...")
    counter_rows = fetch_all_rows("allDataapiInventoryCounters", "Inventory Counters", COUNTER_FIELDS)
    inventory_rows = []
    for r in counter_rows:
        pid = str(r["projectId"])
        if pid not in PROJECT_INDEX:
            continue
        p_idx = PROJECT_INDEX[pid]
        m = material_idx(r.get("resourceGuid"))
        # PO number(s)/description(s) come from the same project+material join
        # Asset Tracking already builds (a stock line has no PO of its own - it is
        # the running position across however many POs have touched that part).
        a = assets.get((p_idx, m), {})
        row = [
            p_idx, m,
            num(r.get("countRequest")), num(r.get("countOrder")), num(r.get("countReceive")),
            num(r.get("countIssue")), num(r.get("countAvailable")), num(r.get("countOnHand")),
            num(r.get("countShip")), num(r.get("countHold")), num(r.get("countDamage")),
            num(r.get("countScrap")), num(r.get("countReturn")),
            S.idx(joined(a.get("pos", set()))), S.idx(joined(a.get("po_descs", set()))),
            S.idx(a.get("po_latest", "")),
        ]
        if any(row[2:13]):  # drop all-zero rows, they carry no signal
            inventory_rows.append(row)
    print(f"  {len(inventory_rows)} non-empty inventory rows (from {len(counter_rows)} counters)")

    # -------------------------------------------------------------------- storage
    print("Fetching Inventory Counters by Location (stock by storage location)...")
    location_rows = fetch_all_rows(
        "allDataapiInventoryCountersByLocations", "Inventory by Location", LOCATION_FIELDS
    )
    storage_rows = []
    fim_seen = set()
    fim_rows = []
    counter_by_guid = {}
    for r in counter_rows:
        counter_by_guid[(str(r["projectId"]), r.get("resourceGuid"))] = r

    for r in location_rows:
        pid = str(r["projectId"])
        if pid in PROJECT_INDEX and r.get("resourceGuid") in fim_guids:
            guid = r["resourceGuid"]
            meta = resource_meta[guid]
            container = resource_meta.get(r.get("containerGuid"), {}).get("name", "")
            c = counter_by_guid.get((pid, guid), {})
            fim_rows.append([
                PROJECT_INDEX[pid],
                material_idx(guid),
                S.idx(r.get("locationName") or ""),
                S.idx(container),
                S.idx(meta["wp"]),
                num(r.get("locationCount")),
                S.idx((r.get("locationStatus") or "").replace("SiteSense.Condition.", "")
                                                    .replace("SiteSense.Status.", "")),
                num(c.get("countReceive")), num(c.get("countIssue")), num(c.get("countAvailable")),
            ])
            fim_seen.add((pid, guid))

    # Every registered FIM part is carried, not just the ones currently holding
    # stock. A part the client has promised but not yet delivered is the thing a
    # project most needs to see, and it would be invisible if this were driven off
    # stock alone.
    for guid in fim_guids:
        meta = resource_meta[guid]
        pid = meta["pid"]
        if pid not in PROJECT_INDEX or (pid, guid) in fim_seen:
            continue
        c = counter_by_guid.get((pid, guid), {})
        fim_rows.append([
            PROJECT_INDEX[pid], material_idx(guid), S.idx(""), S.idx(""),
            S.idx(meta["wp"]), num(c.get("countOnHand")), S.idx(""),
            num(c.get("countReceive")), num(c.get("countIssue")), num(c.get("countAvailable")),
        ])
    no_stock = sum(1 for r in fim_rows if not r[7] and not r[8] and not r[9] and not r[5])
    print(f"  {len(fim_rows)} FIM rows ({len(fim_seen)} in a tracked location, "
          f"{no_stock} registered with no stock recorded)")

    for r in location_rows:
        pid = str(r["projectId"])
        if pid not in PROJECT_INDEX:
            continue
        qty = num(r.get("locationCount"))
        if not qty:
            continue
        storage_rows.append([
            PROJECT_INDEX[pid],
            material_idx(r.get("resourceGuid")),
            S.idx(r.get("locationName") or "(no location)"),
            S.idx((r.get("locationStatus") or "").replace("SiteSense.Condition.", "")
                                                 .replace("SiteSense.Status.", "")),
            qty,
        ])
    print(f"  {len(storage_rows)} storage rows")

    # ------------------------------------------------------------------ transfers
    print("Building Transfer lines (material level)...")
    transfer_rows = []
    for r in transfer_lines:
        pid = str(r["projectId"])
        if pid not in PROJECT_INDEX:
            continue
        p = props(r)
        transfer_rows.append([
            PROJECT_INDEX[pid],
            line_material(r),
            S.idx(r.get("resourceName") or ""),
            S.idx(r.get("resourceStatusName") or ""),
            num(r.get("lineQuantity")),
            S.idx(p.get("MovementFromLocation") or ""),
            S.idx(p.get("MovementToLocation") or ""),
            S.idx((r.get("resourceUpdatedAt") or "")[:10]),
            S.idx((r.get("resourceCreatedAt") or "")[:7]),
        ])
    print(f"  {len(transfer_rows)} transfer lines")

    # ----------------------------------------------------------------- issuing
    # The Issuing Dashboard mirrors SiteSense's own native report, confirmed by
    # generating it directly: Material Request -> Pick Ticket -> Issue Ticket is
    # a real reference chain, each carrying part of the picture. Requested
    # Quantity comes off the Material Request, Picked Quantity and where it was
    # picked from off the Pick Ticket, and Issued Quantity plus who requested it
    # off the Issue Ticket itself. Available Quantity is not stored on any of
    # them; it is the material's current stock position, joined in from the
    # same Inventory Counters used on the Inventory tab.
    print("Building Issuing Dashboard (request -> pick -> issue chain)...")

    def res_props(r):
        out = {}
        for prop in (r.get("resourceProperties") or []):
            if isinstance(prop, dict) and prop.get("Key"):
                out[prop["Key"]] = prop.get("Value")
        return out

    available_by_key = {(row[0], row[1]): row[6] for row in inventory_rows}

    rq_qty = defaultdict(float)   # (projectId, RQ name, material guid) -> requested qty
    for r in material_request_lines:
        guid = r.get("lineResourceGuid") or r.get("lineResourceName")
        rq_qty[(str(r["projectId"]), r.get("resourceName") or "", guid)] += float(r.get("lineQuantity") or 0)

    pt_by_key = {}   # (projectId, IT name, material guid) -> pick ticket info
    for r in pick_ticket_lines:
        guid = r.get("lineResourceGuid") or r.get("lineResourceName")
        it_name = r.get("lineFromReferenceDocumentName") or ""
        key = (str(r["projectId"]), it_name, guid)
        rp = res_props(r)
        lp = props(r)
        entry = pt_by_key.setdefault(key, {"qty": 0.0, "recipient": "", "location": "",
                                            "handover": "", "name": r.get("resourceName") or ""})
        entry["qty"] += float(r.get("lineQuantity") or 0)
        entry["recipient"] = entry["recipient"] or rp.get("Recipient Name", "")
        entry["location"] = entry["location"] or lp.get("BucketAllocation", "")
        entry["handover"] = entry["handover"] or rp.get("Handover Date", "")

    issuing_rows = []
    for r in issue_lines:
        pid = str(r["projectId"])
        if pid not in PROJECT_INDEX:
            continue
        guid = r.get("lineResourceGuid") or r.get("lineResourceName")
        rq_name = r.get("lineFromReferenceDocumentName") or ""
        it_name = r.get("resourceName") or ""
        rp = res_props(r)
        pt = pt_by_key.get((pid, it_name, guid), {})
        m = line_material(r)
        p_idx = PROJECT_INDEX[pid]
        issuing_rows.append([
            p_idx, m,
            S.idx(it_name),
            S.idx(r.get("resourceStatusName") or ""),
            S.idx(rq_name),
            S.idx(pt.get("name", "")),
            S.idx(rp.get("Requested By") or rp.get("Requestor") or ""),
            num(rq_qty.get((pid, rq_name, guid), 0)),
            num(pt.get("qty", 0)),
            num(r.get("lineQuantity")),
            num(available_by_key.get((p_idx, m), 0)),
            S.idx(pt.get("recipient", "")),
            S.idx(pt.get("location", "")),
            S.idx((r.get("resourceCreatedAt") or "")[:10]),
        ])
    print(f"  {len(issuing_rows)} issuing lines")

    # -------------------------------------------------------- document ledger tabs
    ledger_out = {}
    for type_key, label, list_field, domain in LEDGER_TYPES:
        # Reuse already-fetched line data where we have it, to avoid re-querying.
        cached = {
            "purchase_orders": po_lines,
            "receipts": receipt_lines,
            "transfer_notes": transfer_lines,
            "shipments": shipment_lines,
            "material_requests": material_request_lines,
            "pick_tickets": pick_ticket_lines,
        }.get(type_key)
        if cached is not None:
            rows = cached
            print(f"Reusing {label} rows for ledger view")
        else:
            print(f"Fetching {label} (ledger)...")
            try:
                rows = fetch_all_rows(list_field, label, HEADER_FIELDS)
            except (urllib.error.URLError, RuntimeError) as exc:
                print(f"  !! Failed to fetch {label}: {exc}")
                ledger_out[type_key] = {"label": label, "domain": domain, "docs": [], "error": str(exc)}
                continue

        seen = {}
        for r in rows:
            name = r.get("resourceName")
            guid = r.get("resourceGuid") or name
            if guid not in seen:
                seen[guid] = r
        docs = [[
            PROJECT_INDEX[str(r["projectId"])],
            S.idx(r.get("resourceStatusName") or "(no status)"),
            S.idx((r.get("resourceCreatedAt") or "")[:7]),
            S.idx(r.get("resourceName") or ""),
            S.idx((r.get("resourceUpdatedAt") or "")[:10]),
        ] for r in seen.values() if str(r["projectId"]) in PROJECT_INDEX]
        ledger_out[type_key] = {"label": label, "domain": domain, "docs": docs}

    print("Loading region overlay...")
    regions = load_regions()
    print("Loading delivery date overlay...")
    delivery_dates = load_delivery_dates()

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strings": S.list,
        "projects": [{"id": pid, "name": name, "region": regions.get(str(pid), "")}
                     for pid, name in PROJECTS],
        "materials": materials,
        "inventory": inventory_rows,
        "storage": storage_rows,
        "transfers": transfer_rows,
        "assets": asset_rows,
        "receipts": receipt_rows,
        "fim": fim_rows,
        "issuing": issuing_rows,
        "delivery_dates": delivery_dates,
        "ledger_types": ledger_out,
    }

    (DATA_DIR / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    print(f"\nWrote data.json ({(DATA_DIR / 'data.json').stat().st_size:,} bytes, "
          f"{len(S.list):,} interned strings)")

    template = (HERE / "dashboard_template.html").read_text(encoding="utf-8")
    output = template.replace(
        "/*__SITESENSE_DATA__*/",
        "const SITESENSE_DATA = " + json.dumps(data, separators=(",", ":")) + ";",
    )
    out_path = DATA_DIR / "PMO_Dashboard.html"
    out_path.write_text(output, encoding="utf-8")
    print(f"Wrote {out_path} ({out_path.stat().st_size:,} bytes)")

    # Keep the publishable copy in step with the local one. The artifact host
    # supplies its own document wrapper, so this version carries only the title,
    # styles and body. Publishing is still a deliberate step, this just means the
    # source is always current and ready to deploy.
    def _slice(text, start, end, keep=True):
        i = text.index(start); j = text.index(end, i) + len(end)
        return text[i:j] if keep else text[i + len(start):j - len(end)]

    artifact = "\n".join([
        _slice(output, "<title>", "</title>"),
        _slice(output, "<style>", "</style>"),
        _slice(output, "<body>", "</body>", keep=False).strip(),
    ]) + "\n"
    if AUTH_HEADER in artifact or "gateway.sitesensecloud" in artifact:
        raise SystemExit("Refusing to write the artifact copy: it contains credentials.")
    art_path = DATA_DIR / "PMO_Dashboard_artifact.html"
    art_path.write_text(artifact, encoding="utf-8")
    print(f"Wrote {art_path} ({art_path.stat().st_size:,} bytes, publish-ready)")


if __name__ == "__main__":
    main()
