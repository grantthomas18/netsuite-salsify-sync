"""
NetSuite → Salsify CSV Export Script
Pulls inventory item data from NetSuite and uploads a Salsify-compatible CSV to FTP.
Run with --test to preview data locally without uploading.
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import hmac
import hashlib
import base64
import random
import string
import urllib.parse
import urllib.request
from datetime import datetime, timezone
import paramiko  # pip install paramiko

# ─── CONFIG ──────────────────────────────────────────────────────────────────
# NetSuite REST API credentials — set via environment variables or .env file
NS_ACCOUNT_ID   = os.getenv("NS_ACCOUNT_ID")        # e.g. 1234567 (also your SuiteTalk URL prefix)
NS_CONSUMER_KEY = os.getenv("NS_CONSUMER_KEY")
NS_CONSUMER_SEC = os.getenv("NS_CONSUMER_SECRET")
NS_TOKEN        = os.getenv("NS_TOKEN")
NS_TOKEN_SEC    = os.getenv("NS_TOKEN_SECRET")

# FTP config — same server as existing NetSuite script
FTP_HOST      = "salsify.exavault.com"
FTP_USER      = "learning_advantage_salsify"
FTP_PASSWORD  = os.getenv("FTP_PASSWORD")           # Store in env / GitHub Secret
FTP_DIR       = "/Netsuite2"
FTP_FILENAME  = "netsuite_salsify_export.csv"

# Location IDs
LOC_US        = [2]           # Learning Advantage Warehouse
LOC_UK        = [85, 86]      # Learning Advantage UK + Anthony Peters Warehouse

# Salsify CSV column → NetSuite field mapping
FIELD_MAP = {
    "SKU (Netsuite)":                  "itemid",
    "Product Title (Netsuite)":        "displayname",
    "UPC/EAN (Netsuite)":              "upccode",
    "CountryofOrigin":                 "countryofmanufacture",
    "US Product Status (Netsuite)":    "custitem8",
    "US Catalog Release Year (Netsuite)": "custitem25659",
    "UK Product Status (Netsuite)":    "custitem25668",
    "UK Catalog Release Year (Netsuite)": "custitem25667",
    "HS Code":                         "custitem25661",
    "HTS Code":                        "custitem25662",
}

# Price level IDs in NetSuite
# Wholesale = Base Price (pricetype ID 1 = standard/base price)
# MSRP      = Price Level ID 5
# Amazon    = Price Level ID 7
PRICE_LEVEL_WHOLESALE_ID = 1
PRICE_LEVEL_MSRP_ID      = 5
PRICE_LEVEL_AMAZON_ID    = 7

# ─── NETSUITE OAUTH 1.0 ───────────────────────────────────────────────────────
def _nonce(length=11):
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def _oauth_header(method, url, extra_params=None):
    """Generate TBA OAuth 1.0 Authorization header for NetSuite."""
    ts = str(int(time.time()))
    nc = _nonce()
    base_params = {
        "oauth_consumer_key":     NS_CONSUMER_KEY,
        "oauth_nonce":            nc,
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp":        ts,
        "oauth_token":            NS_TOKEN,
        "oauth_version":          "1.0",
    }
    all_params = {**base_params, **(extra_params or {})}
    sorted_params = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(all_params.items())
    )
    base_string = "&".join([
        method.upper(),
        urllib.parse.quote(url, safe=""),
        urllib.parse.quote(sorted_params, safe=""),
    ])
    signing_key = f"{urllib.parse.quote(NS_CONSUMER_SEC, safe='')}&{urllib.parse.quote(NS_TOKEN_SEC, safe='')}"
    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha256).digest()
    ).decode()
    auth_params = {**base_params, "oauth_signature": signature, "realm": NS_ACCOUNT_ID}
    header = "OAuth " + ", ".join(
        f'{k}="{urllib.parse.quote(str(v), safe="")}"'
        for k, v in sorted(auth_params.items())
    )
    return header

def ns_get(path, params=None):
    """Make an authenticated GET request to NetSuite REST API."""
    account_slug = NS_ACCOUNT_ID.replace("_", "-").lower()
    base_url = f"https://{account_slug}.suitetalk.api.netsuite.com{path}"
    query = urllib.parse.urlencode(params or {})
    full_url = f"{base_url}?{query}" if query else base_url
    auth = _oauth_header("GET", base_url, params)
    req = urllib.request.Request(full_url, headers={
        "Authorization": auth,
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

# ─── DATA FETCHING ────────────────────────────────────────────────────────────
def fetch_inventory_items():
    """Fetch all inventory items with required fields using SuiteQL."""
    query = """
        SELECT
            i.id,
            i.itemid,
            i.displayname,
            i.upccode,
            i.countryofmanufacture,
            i.custitem8,
            i.custitem25659,
            i.custitem25668,
            i.custitem25667,
            i.custitem25661,
            i.custitem25662
        FROM item i
        WHERE i.itemtype = 'InvtPart'
          AND i.isinactive = 'F'
        ORDER BY i.itemid
    """
    items = []
    offset = 0
    limit = 1000
    while True:
        result = ns_get("/services/rest/query/v1/suiteql", {
            "q": query, "limit": limit, "offset": offset
        })
        rows = result.get("items", [])
        items.extend(rows)
        if not result.get("hasMore", False):
            break
        offset += limit
    print(f"  Fetched {len(items)} inventory items from NetSuite")
    return items

def fetch_prices(item_ids):
    """Fetch pricing by price level ID for all items.
       Wholesale = Base Price (ID 1), MSRP = ID 5, Amazon = ID 7
    """
    if not item_ids:
        return {}
    ids_str = ", ".join(str(i) for i in item_ids)
    query = f"""
        SELECT
            ip.item,
            ip.pricetype,
            ip.unitprice
        FROM pricing ip
        WHERE ip.item IN ({ids_str})
          AND ip.currency = 1
          AND ip.quantity = 0
          AND ip.pricetype IN ({PRICE_LEVEL_WHOLESALE_ID}, {PRICE_LEVEL_MSRP_ID}, {PRICE_LEVEL_AMAZON_ID})
    """
    result = ns_get("/services/rest/query/v1/suiteql", {"q": query, "limit": 5000})
    prices = {}
    level_map = {
        PRICE_LEVEL_WHOLESALE_ID: "wholesale",
        PRICE_LEVEL_MSRP_ID:      "msrp",
        PRICE_LEVEL_AMAZON_ID:    "amazon",
    }
    for row in result.get("items", []):
        item_id  = str(row["item"])
        level    = level_map.get(int(row["pricetype"]))
        price    = row["unitprice"]
        if item_id not in prices:
            prices[item_id] = {}
        if level:
            prices[item_id][level] = price
    print(f"  Fetched prices for {len(prices)} items")
    return prices

def fetch_inventory(item_ids, location_ids):
    """Fetch quantity available and on order for given locations."""
    if not item_ids:
        return {}
    ids_str  = ", ".join(str(i) for i in item_ids)
    locs_str = ", ".join(str(l) for l in location_ids)
    query = f"""
        SELECT
            il.item,
            SUM(il.quantityavailable) AS qty_available,
            SUM(il.quantityonorder)   AS qty_on_order
        FROM inventorylocation il
        WHERE il.item IN ({ids_str})
          AND il.location IN ({locs_str})
        GROUP BY il.item
    """
    result = ns_get("/services/rest/query/v1/suiteql", {"q": query, "limit": 5000})
    inv = {}
    for row in result.get("items", []):
        inv[str(row["item"])] = {
            "qty_available": row.get("qty_available", 0),
            "qty_on_order":  row.get("qty_on_order", 0),
        }
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

def build_csv(items, prices, inv_us, inv_uk):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=SALSIFY_COLUMNS)
    writer.writeheader()
    for item in items:
        iid  = str(item.get("id", ""))
        p    = prices.get(iid, {})
        us   = inv_us.get(iid, {})
        uk   = inv_uk.get(iid, {})
        row  = {
            "SKU (Netsuite)":                     item.get("itemid", ""),
            "Product Title (Netsuite)":            item.get("displayname", ""),
            "UPC/EAN (Netsuite)":                  item.get("upccode", ""),
            "Wholesale Price (Netsuite)":           p.get("wholesale", ""),
            "MSRP Price (Netsuite)":               p.get("msrp", ""),
            "Amazon Price (Netsuite)":             p.get("amazon", ""),
            "US Product Status (Netsuite)":        item.get("custitem8", ""),
            "US Catalog Release Year (Netsuite)":  item.get("custitem25659", ""),
            "UK Product Status (Netsuite)":        item.get("custitem25668", ""),
            "UK Catalog Release Year (Netsuite)":  item.get("custitem25667", ""),
            "CountryofOrigin":                     item.get("countryofmanufacture", ""),
            "Inventory Available US (Netsuite)":   us.get("qty_available", ""),
            "Inventory On Order US (Netsuite)":    us.get("qty_on_order", ""),
            "Inventory Available UK (Netsuite)":   uk.get("qty_available", ""),
            "Inventory On Order UK (Netsuite)":    uk.get("qty_on_order", ""),
            "HS Code":                             item.get("custitem25661", ""),
            "HTS Code":                            item.get("custitem25662", ""),
        }
        writer.writerow(row)
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
def main(test_mode=False, preview_rows=10):
    print(f"\n{'='*60}")
    print(f"NetSuite → Salsify Export  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Mode: {'TEST (no FTP upload)' if test_mode else 'LIVE'}")
    print(f"{'='*60}\n")

    # Validate credentials
    missing = [v for v in ["NS_ACCOUNT_ID","NS_CONSUMER_KEY","NS_CONSUMER_SECRET","NS_TOKEN","NS_TOKEN_SECRET"] if not os.getenv(v)]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        sys.exit(1)
    if not test_mode and not FTP_PASSWORD:
        print("❌ Missing FTP_PASSWORD environment variable")
        sys.exit(1)

    print("Step 1/4 — Fetching inventory items from NetSuite...")
    items = fetch_inventory_items()

    item_ids = [item["id"] for item in items]

    print("Step 2/4 — Fetching prices...")
    prices = fetch_prices(item_ids)

    print("Step 3/4 — Fetching inventory by location...")
    inv_us = fetch_inventory(item_ids, LOC_US)
    inv_uk = fetch_inventory(item_ids, LOC_UK)

    print("Step 4/4 — Building CSV...")
    csv_content = build_csv(items, prices, inv_us, inv_uk)

    rows = csv_content.strip().split("\n")
    print(f"\n  Total rows (excl. header): {len(rows) - 1}")

    if test_mode:
        print(f"\n{'─'*60}")
        print(f"TEST MODE — Preview of first {preview_rows} rows:")
        print(f"{'─'*60}")
        # Pretty print as table
        reader = csv.DictReader(io.StringIO(csv_content))
        preview = [row for _, row in zip(range(preview_rows), reader)]
        for i, row in enumerate(preview, 1):
            print(f"\n  --- Row {i} ---")
            for col, val in row.items():
                print(f"  {col:<45} {val}")
        # Save locally
        local_path = f"test_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(local_path, "w") as f:
            f.write(csv_content)
        print(f"\n  ✅ Full CSV saved locally: {local_path}")
        print("  ℹ️  Review the file, then run without --test to upload to FTP.")
    else:
        upload_to_ftp(csv_content)
        print(f"\n✅ Done — {len(rows)-1} items synced to Salsify FTP")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetSuite → Salsify CSV sync")
    parser.add_argument("--test", action="store_true", help="Preview data locally, skip FTP upload")
    parser.add_argument("--preview-rows", type=int, default=10, help="Rows to preview in test mode (default: 10)")
    args = parser.parse_args()
    main(test_mode=args.test, preview_rows=args.preview_rows)
