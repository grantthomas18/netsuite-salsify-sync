"""
NetSuite → Salsify CSV Export Script
Pulls inventory item data from NetSuite and uploads a Salsify-compatible CSV to FTP.
Run with --test to preview data locally without uploading.

Requirements: pip install requests requests-oauthlib paramiko
"""

import argparse
import csv
import io
import os
import sys
from datetime import datetime, timezone

import requests
from requests_oauthlib import OAuth1
import paramiko

# ─── CONFIG ──────────────────────────────────────────────────────────────────
NS_ACCOUNT_ID   = os.getenv("NS_ACCOUNT_ID")       # e.g. 4876915
NS_CONSUMER_KEY = os.getenv("NS_CONSUMER_KEY")
NS_CONSUMER_SEC = os.getenv("NS_CONSUMER_SECRET")
NS_TOKEN        = os.getenv("NS_TOKEN")
NS_TOKEN_SEC    = os.getenv("NS_TOKEN_SECRET")

FTP_HOST     = "salsify.exavault.com"
FTP_USER     = "learning_advantage_salsify"
FTP_PASSWORD = os.getenv("FTP_PASSWORD")
FTP_DIR      = "/Netsuite2"
FTP_FILENAME = "salsify_export.csv"

LOC_US = [2]        # Learning Advantage Warehouse
LOC_UK = [85, 86]   # Learning Advantage UK + Anthony Peters Warehouse

# Price level IDs: Wholesale = Base Price (1), MSRP = ID 5, Amazon = ID 7
PRICE_LEVEL_WHOLESALE_ID = 1
PRICE_LEVEL_MSRP_ID      = 5
PRICE_LEVEL_AMAZON_ID    = 7

# ─── NETSUITE SUITEQL ────────────────────────────────────────────────────────
def get_auth():
    """Build OAuth1 TBA auth object for NetSuite."""
    return OAuth1(
        client_key=NS_CONSUMER_KEY,
        client_secret=NS_CONSUMER_SEC,
        resource_owner_key=NS_TOKEN,
        resource_owner_secret=NS_TOKEN_SEC,
        realm=NS_ACCOUNT_ID,
        signature_method="HMAC-SHA256",
    )

def get_base_url():
    account_slug = NS_ACCOUNT_ID.replace("_", "-").lower()
    return f"https://{account_slug}.suitetalk.api.netsuite.com"

def ns_suiteql(query, limit=1000, offset=0):
    """Execute a SuiteQL query via POST against the NetSuite REST API."""
    url  = f"{get_base_url()}/services/rest/query/v1/suiteql"
    resp = requests.post(
        url,
        params={"limit": limit, "offset": offset},
        json={"q": query},
        headers={"Prefer": "transient"},
        auth=get_auth(),
    )
    if not resp.ok:
        print(f"\n❌ NetSuite API error {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()

# ─── DATA FETCHING ────────────────────────────────────────────────────────────
def fetch_inventory_items():
    """Fetch active inventory items that have a UPC and are flagged for Salsify.
    Joins the custom list for US/UK Product Status to get the label text.
    """
    query = """
        SELECT
            i.id,
            i.itemid,
            i.displayname,
            i.upccode,
            i.countryofmanufacture,
            i.custitem8     AS us_product_status,
            i.custitem25659 AS us_catalog_year,
            i.custitem25668 AS uk_product_status,
            i.custitem25667 AS uk_catalog_year,
            i.custitem25661,
            i.custitem25662
        FROM item i
        WHERE i.itemtype = 'InvtPart'
          AND i.isinactive = 'F'
          AND i.upccode IS NOT NULL
          AND i.custitem25663 = 'Y'
        ORDER BY i.itemid
    """
    items, offset, limit = [], 0, 1000
    while True:
        result = ns_suiteql(query, limit=limit, offset=offset)
        rows = result.get("items", [])
        items.extend(rows)
        if not result.get("hasMore", False):
            break
        offset += limit
    print(f"  Fetched {len(items)} inventory items from NetSuite")
    return items

def ns_suiteql_all(query):
    """Execute a paginated SuiteQL query, returning all rows (max 1000 per page)."""
    rows, offset = [], 0
    while True:
        result = ns_suiteql(query, limit=1000, offset=offset)
        rows.extend(result.get("items", []))
        if not result.get("hasMore", False):
            break
        offset += 1000
    return rows

def diagnose(item_ids):
    """Run diagnostic queries to find correct price level IDs and verify inventory table."""
    print("\n" + "="*60)
    print("DIAGNOSTIC MODE")
    print("="*60)

    # Check what values custitem25663 (Add to Salsify) actually contains
    print("\n--- Distinct values of custitem25663 (Add to Salsify flag) ---")
    query_flag = """
        SELECT DISTINCT i.custitem25663
        FROM item i
        WHERE i.itemtype = 'InvtPart'
          AND i.isinactive = 'F'
          AND i.custitem25663 IS NOT NULL
    """
    try:
        rows_flag = ns_suiteql_all(query_flag)
        print(f"  Values found: {[r.get('custitem25663') for r in rows_flag]}")
        total_q = ns_suiteql_all("SELECT COUNT(*) AS cnt FROM item i WHERE i.itemtype = 'InvtPart' AND i.isinactive = 'F'")
        print(f"  Total active InvtPart items: {total_q[0].get('cnt') if total_q else 'unknown'}")
    except Exception as e:
        print(f"  Error: {e}")

    # Look up custom list entries for custitem8 to find status labels
    # custitem8 stores an integer ID — we look up matching entries in customlist tables
    print("\n--- Sampling custitem8 values from items to find status IDs ---")
    query_sample = """
        SELECT DISTINCT i.custitem8, i.custitem25668
        FROM item i
        WHERE i.itemtype = 'InvtPart'
          AND i.isinactive = 'F'
          AND i.custitem8 IS NOT NULL
        ORDER BY i.custitem8
    """
    try:
        rows_sample = ns_suiteql_all(query_sample)
        print(f"  Distinct custitem8 values: {[r.get('custitem8') for r in rows_sample[:20]]}")
        print(f"  Distinct custitem25668 values: {list(set(r.get('custitem25668') for r in rows_sample))[:20]}")
    except Exception as e:
        print(f"  Error: {e}")

    # Sample one item to check pricing
    sample_id = item_ids[0] if item_ids else None
    if sample_id:
        print(f"\n--- Price levels found for item ID {sample_id} ---")
        query = f"""
            SELECT ip.pricelevel, ip.unitprice, ip.quantity, ip.currency
            FROM pricing ip
            WHERE ip.item = {sample_id}
        """
        rows = ns_suiteql_all(query)
        if rows:
            for r in rows:
                print(f"  pricelevel={r.get('pricelevel')}  price={r.get('unitprice')}  qty={r.get('quantity')}  currency={r.get('currency')}")
        else:
            print("  No pricing rows found for this item.")

        print(f"\n--- Price level names (from pricelevel table) ---")
        query2 = """
            SELECT pl.id, pl.name
            FROM pricelevel pl
            ORDER BY pl.id
        """
        rows2 = ns_suiteql_all(query2)
        for r in rows2:
            print(f"  ID={r.get('id')}  Name={r.get('name')}")

    # Test inventory table
    print(f"\n--- Inventory check for item ID {sample_id}, location 2 ---")
    for table in ["inventorybalance", "inventorylocation", "inventoryitem"]:
        try:
            query = f"SELECT item FROM {table} WHERE item = {sample_id} AND location = 2"
            rows = ns_suiteql_all(query)
            print(f"  Table '{table}': ✅ found {len(rows)} rows")
            break
        except SystemExit:
            print(f"  Table '{table}': ❌ invalid")


    # Diagnose inventorybalance fields for item 10510
    print("\n--- inventorybalance fields for item 10510 at location 2 ---")
    lookup_ib = ns_suiteql_all("SELECT id FROM item WHERE itemid = '10510' AND itemtype = 'InvtPart'")
    ib_item_id = lookup_ib[0]['id'] if lookup_ib else None
    if ib_item_id:
        # Fetch all numeric fields we can
        rows_ib = ns_suiteql_all(f"""
            SELECT ib.quantityavailable, ib.quantityonhand, ib.quantitypicked
            FROM inventorybalance ib
            WHERE ib.item = {ib_item_id} AND ib.location = 2
        """)
        for r in rows_ib:
            print(f"  quantityavailable={r.get('quantityavailable')}  quantityonhand={r.get('quantityonhand')}  quantitypicked={r.get('quantitypicked')}")

    # Diagnose committed qty for item 10510
    print("\n--- Committed SO lines for item 10510 ---")
    lookup2 = ns_suiteql_all("SELECT id FROM item WHERE itemid = '10510' AND itemtype = 'InvtPart'")
    co_item_id = lookup2[0]['id'] if lookup2 else None
    if co_item_id:
        rows_so = ns_suiteql_all(f"""
            SELECT COALESCE(tl.location, t.location) AS eff_loc,
                   tl.quantity, tl.quantitypicked, tl.mainline, tl.itemtype, t.status
            FROM transactionline tl
            INNER JOIN transaction t ON t.id = tl.transaction
            WHERE tl.item = {co_item_id}
              AND t.type = 'SalesOrd'
              AND t.status IN ('B', 'D')
              AND COALESCE(tl.location, t.location) = 2
        """)
        print(f"  Open SO lines at loc 2 (status B/D): {len(rows_so)}")
        total_committed = 0
        for r in rows_so:
            qty = abs(float(r.get('quantity') or 0))
            picked = abs(float(r.get('quantitypicked') or 0))
            remaining = qty - picked
            total_committed += remaining
            print(f"  qty={qty}  picked={picked}  remaining={remaining}  mainline={r.get('mainline')}  itemtype={r.get('itemtype')}  status={r.get('status')}")
        print(f"  Total committed (all lines): {total_committed}")
        print("\n  Committed totals by location:")
        for loc, qty in sorted(totals_so.items()):
            print(f"    Location {loc}: {qty}")

        # Also check what status codes exist on SOs
        rows_status = ns_suiteql_all(f"""
            SELECT DISTINCT t.status
            FROM transactionline tl
            INNER JOIN transaction t ON t.id = tl.transaction
            WHERE tl.item = {co_item_id}
              AND t.type = 'SalesOrd'
        """)
        print(f"\n  All SO statuses for this item: {[r.get('status') for r in rows_status]}")

    # Diagnose on-order: look up item 10510 specifically
    print(f"\n--- Open PO lines for item SKU 10510, broken out by location ---")
    lookup = ns_suiteql_all("SELECT id FROM item WHERE itemid = '10510' AND itemtype = 'InvtPart'")
    po_item_id = lookup[0]['id'] if lookup else sample_id
    print(f"  Item internal ID: {po_item_id}")
    rows_po = ns_suiteql_all(f"""
        SELECT tl.location AS line_loc, t.location AS hdr_loc,
               COALESCE(tl.location, t.location) AS eff_loc,
               tl.quantity, t.status
        FROM transactionline tl
        INNER JOIN transaction t ON t.id = tl.transaction
        WHERE tl.item = {po_item_id}
          AND t.type = 'PurchOrd'
          AND t.status IN ('B', 'D')
    """)
    totals = {}
    for r in rows_po:
        loc = str(r.get('eff_loc') or 'None')
        totals[loc] = totals.get(loc, 0) + float(r.get('quantity') or 0)
        print(f"  line_loc={r.get('line_loc')}  hdr_loc={r.get('hdr_loc')}  eff={r.get('eff_loc')}  qty={r.get('quantity')}")
    if not rows_po:
        print("  No open PO lines found for 10510")
    print("\n  Totals by location:")
    for loc, qty in sorted(totals.items()):
        print(f"    Location {loc}: {qty}")
    print(f"    GRAND TOTAL: {sum(totals.values())}")
    print(f"    US locs: {LOC_US}, UK locs: {LOC_UK}")
    print("\n" + "="*60)
    print("Update PRICE_LEVEL_WHOLESALE_ID / MSRP / AMAZON at top of script")
    print("based on the IDs shown above, then re-run without --diagnose.")
    print("="*60 + "\n")
    sys.exit(0)

def fetch_prices(item_ids):
    """Fetch pricing by price level ID (Wholesale=1, MSRP=5, Amazon=7)."""
    if not item_ids:
        return {}
    prices = {}
    level_map = {
        PRICE_LEVEL_WHOLESALE_ID: "wholesale",
        PRICE_LEVEL_MSRP_ID:      "msrp",
        PRICE_LEVEL_AMAZON_ID:    "amazon",
    }
    chunk_size = 500
    for i in range(0, len(item_ids), chunk_size):
        chunk   = item_ids[i:i + chunk_size]
        ids_str = ", ".join(str(x) for x in chunk)
        query = f"""
            SELECT ip.item, ip.pricelevel, ip.unitprice
            FROM pricing ip
            WHERE ip.item IN ({ids_str})
              AND ip.currency = 1
              AND ip.quantity = 1
              AND ip.pricelevel IN ({PRICE_LEVEL_WHOLESALE_ID}, {PRICE_LEVEL_MSRP_ID}, {PRICE_LEVEL_AMAZON_ID})
        """
        for row in ns_suiteql_all(query):
            item_id = str(row["item"])
            level   = level_map.get(int(row["pricelevel"]))
            if item_id not in prices:
                prices[item_id] = {}
            if level:
                prices[item_id][level] = row["unitprice"]
    print(f"  Fetched prices for {len(prices)} items")
    return prices

def fetch_inventory(item_ids, location_ids):
    """Fetch qty available and on order, summed across given location IDs."""
    if not item_ids:
        return {}
    locs_str = ", ".join(str(l) for l in location_ids)
    inv = {}
    chunk_size = 500
    for i in range(0, len(item_ids), chunk_size):
        chunk   = item_ids[i:i + chunk_size]
        ids_str = ", ".join(str(x) for x in chunk)
        # Quantity On Hand from inventorybalance (used as "Available" in Salsify)
        query_avail = f"""
            SELECT
                ib.item,
                SUM(ib.quantityonhand) AS qty_available
            FROM inventorybalance ib
            WHERE ib.item IN ({ids_str})
              AND ib.location IN ({locs_str})
            GROUP BY ib.item
        """
        for row in ns_suiteql_all(query_avail):
            item_id = str(row["item"])
            if item_id not in inv:
                inv[item_id] = {"qty_available": 0, "qty_on_order": 0}
            inv[item_id]["qty_available"] = float(row.get("qty_available") or 0)

        # quantityonorder: remaining unreceived qty on open PO lines per location
        # quantitybilled = qty received/billed on PO lines in SuiteQL
        query_order = f"""
            SELECT
                tl.item,
                SUM(tl.quantity - NVL(tl.quantitybilled, 0)) AS qty_on_order
            FROM transactionline tl
            INNER JOIN transaction t ON t.id = tl.transaction
            WHERE tl.item IN ({ids_str})
              AND COALESCE(tl.location, t.location) IN ({locs_str})
              AND t.type = 'PurchOrd'
              AND t.status IN ('B', 'D')
              AND tl.quantity > NVL(tl.quantitybilled, 0)
            GROUP BY tl.item
        """
        for row in ns_suiteql_all(query_order):
            item_id = str(row["item"])
            if item_id not in inv:
                inv[item_id] = {"qty_available": 0, "qty_on_order": 0}
            inv[item_id]["qty_on_order"] = row.get("qty_on_order", 0)
    return inv

# ─── BUILD CSV ────────────────────────────────────────────────────────────────
SALSIFY_COLUMNS = [
    "SKU (Netsuite)",
    "Product Title (Netsuite)",
    "UPC/EAN (Netsuite)",
    "Wholesale Price (Netsuite)",
    "MSRP Price (Netsuite)",
    "Amazon Price (Netsuite)",
    "US Product Status (Netsuite)",
    "US Catalog Release Year (Netsuite)",
    "UK Product Status (Netsuite)",
    "UK Catalog Release Year (Netsuite)",
    "CountryofOrigin",
    "Inventory Available US (Netsuite)",
    "Inventory On Order US (Netsuite)",
    "Inventory Available UK (Netsuite)",
    "Inventory On Order UK (Netsuite)",
    "HS Code",
    "HTS Code",
]

# Translation maps applied in Python (SuiteQL item table doesn't support CASE)
COUNTRY_MAP = {
    "TW": "Taiwan", "CN": "China", "IT": "Italy", "VN": "Vietnam",
    "AT": "Austria", "TH": "Thailand", "IN": "India",
    "US": "United States", "GB": "United Kingdom", "IL": "Israel",
}

STATUS_MAP = {
    1:  "Stocked / Active",
    2:  "Discontinued",
    3:  "Obsolete",
    4:  "Stocked Exclusive",
    5:  "Work Order",
    6:  "Amazon Only",
    7:  "Product Development",
    8:  "Component",
    9:  "Target DI",
    10: "Discontinued stock in warehouse",
    11: "Linda Thomas",
    12: "Temporarily Unavailable",
    13: "Amazon Only Work Order",
    14: "Held for Scholastic",
    15: "Not available",
}

def translate_status(raw):
    if raw is None or raw == "" or raw != raw:  # handles None and NaN
        return ""
    try:
        return STATUS_MAP.get(int(float(str(raw))), str(raw))
    except (ValueError, TypeError):
        return str(raw)

def build_csv(items, prices, inv_us, inv_uk):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=SALSIFY_COLUMNS)
    writer.writeheader()
    for item in items:
        iid     = str(item.get("id", ""))
        p       = prices.get(iid, {})
        us      = inv_us.get(iid, {})
        uk      = inv_uk.get(iid, {})
        country = item.get("countryofmanufacture") or ""
        writer.writerow({
            "SKU (Netsuite)":                     item.get("itemid", ""),
            "Product Title (Netsuite)":            item.get("displayname", ""),
            "UPC/EAN (Netsuite)":                  item.get("upccode", ""),
            "Wholesale Price (Netsuite)":           p.get("wholesale", ""),
            "MSRP Price (Netsuite)":               p.get("msrp", ""),
            "Amazon Price (Netsuite)":             p.get("amazon", ""),
            "US Product Status (Netsuite)":        translate_status(item.get("us_product_status")),
            "US Catalog Release Year (Netsuite)":  item.get("us_catalog_year", ""),
            "UK Product Status (Netsuite)":        translate_status(item.get("uk_product_status")),
            "UK Catalog Release Year (Netsuite)":  item.get("uk_catalog_year", ""),
            "CountryofOrigin":                     COUNTRY_MAP.get(country, country),
            "Inventory Available US (Netsuite)":   int(us["qty_available"]) if us.get("qty_available") not in (None, "") else "",
            "Inventory On Order US (Netsuite)":    int(us["qty_on_order"])   if us.get("qty_on_order")   not in (None, "") else "",
            "Inventory Available UK (Netsuite)":   int(uk["qty_available"]) if uk.get("qty_available") not in (None, "") else "",
            "Inventory On Order UK (Netsuite)":    int(uk["qty_on_order"])   if uk.get("qty_on_order")   not in (None, "") else "",
            "HS Code":                             item.get("custitem25661", ""),
            "HTS Code":                            item.get("custitem25662", ""),
        })
    return output.getvalue()

# ─── FTP UPLOAD ───────────────────────────────────────────────────────────────
def upload_to_ftp(csv_content):
    print(f"\n  Connecting to FTP: {FTP_HOST} ...")
    transport = paramiko.Transport((FTP_HOST, 22))
    transport.connect(username=FTP_USER, password=FTP_PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    remote_path = f"{FTP_DIR}/{FTP_FILENAME}"
    with sftp.open(remote_path, "w") as f:
        f.write(csv_content)
    sftp.close()
    transport.close()
    print(f"  ✅ Uploaded to {FTP_HOST}{remote_path}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main(test_mode=False, preview_rows=10, diagnose_mode=False):
    print(f"\n{'='*60}")
    print(f"NetSuite → Salsify Export  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Mode: {'TEST (no FTP upload)' if test_mode else 'LIVE'}")
    print(f"{'='*60}\n")

    missing = [v for v in ["NS_ACCOUNT_ID", "NS_CONSUMER_KEY", "NS_CONSUMER_SECRET", "NS_TOKEN", "NS_TOKEN_SECRET"] if not os.getenv(v)]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        sys.exit(1)
    if not test_mode and not diagnose_mode and not FTP_PASSWORD:
        print("❌ Missing FTP_PASSWORD environment variable")
        sys.exit(1)

    print("Step 1/4 — Fetching inventory items from NetSuite...")
    items = fetch_inventory_items()
    item_ids = [item["id"] for item in items]

    if diagnose_mode:
        diagnose(item_ids)

    print("Step 2/4 — Fetching prices...")
    prices = fetch_prices(item_ids)

    print("Step 3/4 — Fetching inventory by location...")
    inv_us = fetch_inventory(item_ids, LOC_US)
    inv_uk = fetch_inventory(item_ids, LOC_UK)

    print("Step 4/4 — Building CSV...")
    csv_content = build_csv(items, prices, inv_us, inv_uk)
    row_count = csv_content.strip().count("\n")
    print(f"\n  Total rows (excl. header): {row_count}")

    if test_mode:
        print(f"\n{'─'*60}")
        print(f"TEST MODE — Preview of first {preview_rows} rows:")
        print(f"{'─'*60}")
        reader  = csv.DictReader(io.StringIO(csv_content))
        preview = [row for _, row in zip(range(preview_rows), reader)]
        for i, row in enumerate(preview, 1):
            print(f"\n  --- Row {i} ---")
            for col, val in row.items():
                print(f"  {col:<45} {val}")
        local_path = f"test_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(local_path, "w", newline="", encoding="utf-8") as f:
            f.write(csv_content)
        print(f"\n  ✅ Full CSV saved locally: {local_path}")
        print("  ℹ️  Review the file, then run without --test to upload to FTP.")
    else:
        upload_to_ftp(csv_content)
        print(f"\n✅ Done — {row_count} items synced to Salsify FTP")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetSuite → Salsify CSV sync")
    parser.add_argument("--test", action="store_true", help="Preview data locally, skip FTP upload")
    parser.add_argument("--preview-rows", type=int, default=10, help="Rows to preview in test mode (default: 10)")
    parser.add_argument("--diagnose", action="store_true", help="Check price level IDs and inventory table in your NetSuite")
    args = parser.parse_args()
    main(test_mode=args.test, preview_rows=args.preview_rows, diagnose_mode=args.diagnose)
