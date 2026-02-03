#!/usr/bin/env python3
"""
WYN → Shopify Product Sync

Compares a new "What You Need" vendor catalogue (Shopify-format CSV) against
the products currently in a Shopify store, then:

  1. Creates products that exist in the catalogue but not in Shopify.
  2. Updates images on existing products where the catalogue has new images.
  3. Reports on unchanged products.

Usage:
    # Dry run (default) — shows what *would* happen, changes nothing:
    python sync.py

    # Live run — actually creates/updates products:
    python sync.py --live

    # Compare only — just print the diff report:
    python sync.py --compare-only

    # Verify current state matches catalogue:
    python sync.py --verify
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from catalogue import (
    Product,
    SyncPlan,
    compare_catalogues,
    parse_shopify_csv,
    product_to_shopify_payload,
)
from shopify_api import ShopifyClient, ShopifyAPIError

# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / f"sync_{datetime.now():%Y%m%d_%H%M%S}.log"),
    ],
)
logger = logging.getLogger("sync")


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

def load_config() -> dict:
    load_dotenv()
    required = ["SHOPIFY_STORE_URL", "SHOPIFY_ACCESS_TOKEN"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        logger.error("Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    return {
        "store_url": os.getenv("SHOPIFY_STORE_URL"),
        "access_token": os.getenv("SHOPIFY_ACCESS_TOKEN"),
        "shopify_csv": os.getenv("SHOPIFY_EXPORT_CSV", "data/shopify_export.csv"),
        "catalogue_csv": os.getenv("WYN_CATALOGUE_CSV", "data/wyn_catalogue.csv"),
        "vendor": os.getenv("VENDOR_NAME", "What You Need"),
    }


# ------------------------------------------------------------------
# Core operations
# ------------------------------------------------------------------

def build_plan(cfg: dict) -> SyncPlan:
    """Parse both CSVs and return a SyncPlan."""
    logger.info("Parsing Shopify export: %s", cfg["shopify_csv"])
    existing = parse_shopify_csv(cfg["shopify_csv"], vendor_filter=cfg["vendor"])

    logger.info("Parsing new catalogue: %s", cfg["catalogue_csv"])
    catalogue = parse_shopify_csv(cfg["catalogue_csv"], vendor_filter=cfg["vendor"])

    logger.info("Comparing catalogues…")
    plan = compare_catalogues(existing, catalogue)
    return plan


def print_plan(plan: SyncPlan):
    """Pretty-print the sync plan."""
    print("\n" + "=" * 60)
    print("  SYNC PLAN")
    print("=" * 60)
    print(plan.summary)
    print()

    if plan.to_create:
        print("--- NEW PRODUCTS TO CREATE ---")
        for p in plan.to_create:
            sku_list = ", ".join(v.sku for v in p.variants if v.sku) or "(no SKU)"
            print(f"  + {p.title}  [{sku_list}]  ({len(p.images)} images)")
        print()

    if plan.to_update_images:
        print("--- PRODUCTS NEEDING IMAGE UPDATES ---")
        for old, new in plan.to_update_images:
            print(f"  ~ {old.title}")
            print(f"      old images: {len(old.images)}  →  new images: {len(new.images)}")
        print()

    if plan.unchanged:
        print(f"--- {len(plan.unchanged)} UNCHANGED PRODUCTS (no action needed) ---")
        for h in plan.unchanged[:10]:
            print(f"    {h}")
        if len(plan.unchanged) > 10:
            print(f"    … and {len(plan.unchanged) - 10} more")
        print()

    print("=" * 60)


def execute_plan(client: ShopifyClient, plan: SyncPlan, cfg: dict):
    """Execute the sync plan against the live Shopify store."""

    # ------- Phase 1: Create new products -------
    if plan.to_create:
        logger.info("Phase 1: Creating %d new products…", len(plan.to_create))
        for i, product in enumerate(plan.to_create, 1):
            logger.info("  [%d/%d] Creating: %s", i, len(plan.to_create), product.title)
            payload = product_to_shopify_payload(product)
            try:
                created = client.create_product(payload)
                logger.info("    ✓ Created (id=%s)", created["id"])
            except ShopifyAPIError as e:
                logger.error("    ✗ Failed to create %s: %s", product.title, e)
    else:
        logger.info("Phase 1: No new products to create.")

    # ------- Phase 2: Update images -------
    if plan.to_update_images:
        logger.info("Phase 2: Updating images on %d products…", len(plan.to_update_images))

        # We need to look up Shopify product IDs by handle.
        # Fetch all vendor products from the API once.
        live_products = client.get_all_products(vendor=cfg["vendor"])
        handle_to_id = {p["handle"]: p["id"] for p in live_products}

        for i, (old_prod, new_prod) in enumerate(plan.to_update_images, 1):
            logger.info("  [%d/%d] Updating images: %s", i, len(plan.to_update_images), old_prod.title)
            product_id = handle_to_id.get(old_prod.handle)
            if not product_id:
                logger.error("    ✗ Could not find Shopify ID for handle=%s", old_prod.handle)
                continue
            try:
                client.replace_product_images(product_id, new_prod.images)
                logger.info("    ✓ Replaced %d images with %d new images",
                            len(old_prod.images), len(new_prod.images))
            except ShopifyAPIError as e:
                logger.error("    ✗ Failed to update images for %s: %s", old_prod.title, e)
    else:
        logger.info("Phase 2: No image updates needed.")


def verify(client: ShopifyClient, cfg: dict):
    """
    Verify that the Shopify store now matches the catalogue.
    Fetches live products and compares against the catalogue CSV.
    """
    logger.info("Verification: fetching live products from Shopify…")
    live_products = client.get_all_products(vendor=cfg["vendor"])
    live_handles = {p["handle"] for p in live_products}

    logger.info("Verification: parsing catalogue…")
    catalogue = parse_shopify_csv(cfg["catalogue_csv"], vendor_filter=cfg["vendor"])

    missing = []
    image_mismatch = []

    for handle, cat_prod in catalogue.items():
        if handle not in live_handles:
            missing.append(cat_prod)
            continue
        # Check image count (rough check — exact URL comparison is unreliable
        # after Shopify CDN processing)
        live = next(p for p in live_products if p["handle"] == handle)
        live_img_count = len(live.get("images", []))
        cat_img_count = len(cat_prod.images)
        if live_img_count != cat_img_count:
            image_mismatch.append((handle, live_img_count, cat_img_count))

    print("\n" + "=" * 60)
    print("  VERIFICATION REPORT")
    print("=" * 60)
    print(f"  Catalogue products:   {len(catalogue)}")
    print(f"  Live Shopify products: {len(live_products)}")
    print()

    if missing:
        print(f"  ✗ {len(missing)} products MISSING from Shopify:")
        for p in missing:
            print(f"      - {p.title} ({p.handle})")
    else:
        print("  ✓ All catalogue products exist in Shopify.")

    if image_mismatch:
        print(f"\n  ⚠ {len(image_mismatch)} products with image count mismatch:")
        for handle, live_n, cat_n in image_mismatch:
            print(f"      - {handle}: Shopify has {live_n}, catalogue has {cat_n}")
    else:
        print("  ✓ Image counts match for all products.")

    print("=" * 60 + "\n")

    if missing or image_mismatch:
        return False
    return True


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync 'What You Need' vendor catalogue to Shopify.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="Execute changes against the live Shopify store.")
    mode.add_argument("--compare-only", action="store_true",
                      help="Only compare catalogues and print the diff (no API calls).")
    mode.add_argument("--verify", action="store_true",
                      help="Verify Shopify matches the catalogue (read-only API calls).")

    parser.add_argument("--shopify-csv", help="Override path to Shopify export CSV.")
    parser.add_argument("--catalogue-csv", help="Override path to new catalogue CSV.")
    parser.add_argument("--vendor", help="Override vendor name filter.")

    args = parser.parse_args()
    cfg = load_config()

    # Allow CLI overrides
    if args.shopify_csv:
        cfg["shopify_csv"] = args.shopify_csv
    if args.catalogue_csv:
        cfg["catalogue_csv"] = args.catalogue_csv
    if args.vendor:
        cfg["vendor"] = args.vendor

    # ------------------------------------------------------------------
    # Compare-only mode
    # ------------------------------------------------------------------
    if args.compare_only:
        plan = build_plan(cfg)
        print_plan(plan)
        return

    # ------------------------------------------------------------------
    # Verify mode
    # ------------------------------------------------------------------
    if args.verify:
        client = ShopifyClient(cfg["store_url"], cfg["access_token"])
        client.test_connection()
        ok = verify(client, cfg)
        sys.exit(0 if ok else 1)

    # ------------------------------------------------------------------
    # Sync mode (dry-run or live)
    # ------------------------------------------------------------------
    plan = build_plan(cfg)
    print_plan(plan)

    total_actions = len(plan.to_create) + len(plan.to_update_images)
    if total_actions == 0:
        logger.info("Nothing to do — store is already in sync.")
        return

    if not args.live:
        print("This was a DRY RUN. No changes were made.")
        print("Re-run with --live to apply changes.\n")
        return

    # Live execution
    client = ShopifyClient(cfg["store_url"], cfg["access_token"])
    shop = client.test_connection()
    logger.info("Connected to: %s", shop["name"])

    print(f"\nAbout to make {total_actions} change(s) to {shop['name']}.")
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    execute_plan(client, plan, cfg)

    # Post-sync verification
    print("\nRunning post-sync verification…")
    ok = verify(client, cfg)
    if ok:
        logger.info("Sync complete — all products verified.")
    else:
        logger.warning("Sync complete but verification found discrepancies. Check the report above.")


if __name__ == "__main__":
    main()
