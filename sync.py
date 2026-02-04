#!/usr/bin/env python3
"""
WYN -> Shopify Product Sync

Compares a new "What You Need" vendor catalogue (Shopify-format CSV) against
the products currently in a Shopify store, then:

  1. Creates products that exist in the catalogue but not in Shopify.
  2. Updates images on existing products where the catalogue has new images.
  3. Syncs prices and costs where they differ.
  4. Auto-tags new products with the Oil Slick taxonomy.
  5. Publishes new products to the Online Store sales channel.
  6. Runs a verification pass and writes a JSON report.

Usage:
    # Dry run (default) -- shows what *would* happen, changes nothing:
    python sync.py

    # Live run -- actually creates/updates products:
    python sync.py --live

    # Live run in CI (no interactive prompt):
    python sync.py --live --yes

    # Fetch current state from API instead of a CSV export:
    python sync.py --from-api

    # Compare only -- just print the diff report:
    python sync.py --compare-only

    # Verify current state matches catalogue:
    python sync.py --verify
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from catalogue import (
    Product,
    SyncPlan,
    compare_catalogues,
    parse_shopify_csv,
    product_to_shopify_payload,
    products_from_api,
)
from shopify_api import ShopifyClient, ShopifyAPIError
from tags import generate_tags, calculate_retail_price

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

    # Support both SHOPIFY_STORE_URL and SHOPIFY_STORE (GitHub Actions compat)
    store_url = os.getenv("SHOPIFY_STORE_URL") or os.getenv("SHOPIFY_STORE", "")
    access_token = os.getenv("SHOPIFY_ACCESS_TOKEN", "")

    if not store_url or not access_token:
        logger.error("Missing SHOPIFY_STORE_URL (or SHOPIFY_STORE) and/or SHOPIFY_ACCESS_TOKEN.")
        logger.error("Set them as env vars or in a .env file (see .env.example).")
        sys.exit(1)

    return {
        "store_url": store_url,
        "access_token": access_token,
        "shopify_csv": os.getenv("SHOPIFY_EXPORT_CSV", "data/shopify_export.csv"),
        "catalogue_csv": os.getenv("WYN_CATALOGUE_CSV", "data/wyn_catalogue.csv"),
        "vendor": os.getenv("VENDOR_NAME", "What You Need"),
    }


# ------------------------------------------------------------------
# Core operations
# ------------------------------------------------------------------

def build_plan(cfg: dict, client: ShopifyClient = None, from_api: bool = False) -> SyncPlan:
    """Parse both sources and return a SyncPlan."""

    # --- Existing products (Shopify state) ---
    if from_api and client:
        logger.info("Fetching current products from Shopify API (vendor=%s)...", cfg["vendor"])
        api_products = client.get_all_products(vendor=cfg["vendor"])
        existing = products_from_api(api_products)
    else:
        logger.info("Parsing Shopify export CSV: %s", cfg["shopify_csv"])
        existing = parse_shopify_csv(cfg["shopify_csv"], vendor_filter=cfg["vendor"])

    # --- New catalogue ---
    logger.info("Parsing new catalogue: %s", cfg["catalogue_csv"])
    catalogue = parse_shopify_csv(cfg["catalogue_csv"], vendor_filter=cfg["vendor"])

    logger.info("Comparing %d existing vs %d catalogue products...",
                len(existing), len(catalogue))
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

    if plan.to_update:
        print("--- PRODUCTS NEEDING UPDATES ---")
        for old, new, diff in plan.to_update:
            print(f"  ~ {old.title}  [{diff}]")
            if diff.images_changed:
                print(f"      images: {len(old.images)} -> {len(new.images)}")
        print()

    if plan.unchanged:
        print(f"--- {len(plan.unchanged)} UNCHANGED PRODUCTS (no action needed) ---")
        for h in plan.unchanged[:10]:
            print(f"    {h}")
        if len(plan.unchanged) > 10:
            print(f"    ... and {len(plan.unchanged) - 10} more")
        print()

    print("=" * 60)


def execute_plan(
    client: ShopifyClient,
    plan: SyncPlan,
    cfg: dict,
    auto_tag: bool = True,
    publish: bool = True,
    set_prices: bool = True,
) -> dict:
    """
    Execute the sync plan against the live Shopify store.
    Returns a report dict.
    """
    report = {
        "created": [],
        "images_updated": [],
        "prices_updated": [],
        "costs_updated": [],
        "published": [],
        "errors": [],
    }

    # ------- Phase 1: Create new products -------
    created_ids = []
    if plan.to_create:
        logger.info("Phase 1: Creating %d new products...", len(plan.to_create))
        for i, product in enumerate(plan.to_create, 1):
            logger.info("  [%d/%d] Creating: %s", i, len(plan.to_create), product.title)

            # Auto-tag
            if auto_tag:
                product.tags = generate_tags(product.title, product.tags, product.product_type)
                logger.info("    Tags: %s", product.tags)

            # Calculate retail prices from cost if no price set
            if set_prices:
                for v in product.variants:
                    if v.cost and (not v.price or v.price == "0.00"):
                        retail = calculate_retail_price(v.cost)
                        if retail:
                            v.price = retail
                            logger.info("    Set price for SKU %s: cost=%s -> retail=%s",
                                       v.sku, v.cost, retail)

            payload = product_to_shopify_payload(product)
            try:
                created = client.create_product(payload)
                created_ids.append(created["id"])
                report["created"].append({
                    "title": product.title,
                    "handle": product.handle,
                    "shopify_id": created["id"],
                    "variants": len(created.get("variants", [])),
                    "images": len(created.get("images", [])),
                })
                logger.info("    Created (id=%s)", created["id"])
            except ShopifyAPIError as e:
                msg = f"Failed to create {product.title}: {e}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
    else:
        logger.info("Phase 1: No new products to create.")

    # ------- Phase 2: Update images -------
    image_updates = plan.to_update_images
    if image_updates:
        logger.info("Phase 2: Updating images on %d products...", len(image_updates))

        # Build handle -> Shopify ID map
        handle_to_id = _get_handle_to_id(client, cfg, plan)

        for i, (old_prod, new_prod, diff) in enumerate(image_updates, 1):
            logger.info("  [%d/%d] Updating images: %s", i, len(image_updates), old_prod.title)
            product_id = old_prod.shopify_id or handle_to_id.get(old_prod.handle)
            if not product_id:
                msg = f"Could not find Shopify ID for handle={old_prod.handle}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
                continue
            try:
                client.replace_product_images(product_id, new_prod.images)
                report["images_updated"].append({
                    "title": old_prod.title,
                    "handle": old_prod.handle,
                    "old_count": len(old_prod.images),
                    "new_count": len(new_prod.images),
                })
                logger.info("    Replaced %d -> %d images",
                           len(old_prod.images), len(new_prod.images))
            except ShopifyAPIError as e:
                msg = f"Failed to update images for {old_prod.title}: {e}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
    else:
        logger.info("Phase 2: No image updates needed.")

    # ------- Phase 3: Update prices -------
    price_updates = plan.to_update_prices
    if price_updates:
        logger.info("Phase 3: Updating prices on %d products...", len(price_updates))

        handle_to_id = _get_handle_to_id(client, cfg, plan)

        for i, (old_prod, new_prod, diff) in enumerate(price_updates, 1):
            logger.info("  [%d/%d] Updating prices: %s", i, len(price_updates), old_prod.title)
            product_id = old_prod.shopify_id or handle_to_id.get(old_prod.handle)
            if not product_id:
                continue

            # Match variants by SKU and update prices
            try:
                live_product = client.get_product(product_id)
                live_variants = {v["sku"]: v for v in live_product.get("variants", []) if v.get("sku")}
                new_variant_map = {v.sku: v for v in new_prod.variants if v.sku}

                for sku, new_v in new_variant_map.items():
                    live_v = live_variants.get(sku)
                    if live_v and new_v.price:
                        client.update_variant(live_v["id"], {"price": new_v.price})
                        logger.info("    Updated price for SKU %s: %s -> %s",
                                   sku, live_v.get("price"), new_v.price)

                report["prices_updated"].append({
                    "title": old_prod.title,
                    "handle": old_prod.handle,
                })
            except ShopifyAPIError as e:
                msg = f"Failed to update prices for {old_prod.title}: {e}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
    else:
        logger.info("Phase 3: No price updates needed.")

    # ------- Phase 4: Update costs -------
    cost_updates = plan.to_update_costs
    if cost_updates:
        logger.info("Phase 4: Updating costs on %d products...", len(cost_updates))

        handle_to_id = _get_handle_to_id(client, cfg, plan)

        for i, (old_prod, new_prod, diff) in enumerate(cost_updates, 1):
            logger.info("  [%d/%d] Updating costs: %s", i, len(cost_updates), old_prod.title)
            product_id = old_prod.shopify_id or handle_to_id.get(old_prod.handle)
            if not product_id:
                continue

            try:
                live_product = client.get_product(product_id)
                live_variants = {v["sku"]: v for v in live_product.get("variants", []) if v.get("sku")}
                new_variant_map = {v.sku: v for v in new_prod.variants if v.sku}

                for sku, new_v in new_variant_map.items():
                    live_v = live_variants.get(sku)
                    if live_v and new_v.cost:
                        inv_id = live_v.get("inventory_item_id")
                        if inv_id:
                            client.update_inventory_item_cost(inv_id, new_v.cost)
                            logger.info("    Updated cost for SKU %s: -> %s", sku, new_v.cost)

                report["costs_updated"].append({
                    "title": old_prod.title,
                    "handle": old_prod.handle,
                })
            except ShopifyAPIError as e:
                msg = f"Failed to update costs for {old_prod.title}: {e}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
    else:
        logger.info("Phase 4: No cost updates needed.")

    # ------- Phase 5: Publish new products -------
    if publish and created_ids:
        logger.info("Phase 5: Publishing %d new products to Online Store...", len(created_ids))
        try:
            count = client.publish_products_to_online_store(created_ids)
            report["published"] = [{"count": count, "total": len(created_ids)}]
            logger.info("    Published %d/%d products", count, len(created_ids))
        except ShopifyAPIError as e:
            msg = f"Failed to publish products: {e}"
            logger.error("    %s", msg)
            report["errors"].append(msg)
    else:
        logger.info("Phase 5: No products to publish.")

    return report


# Cache for handle -> ID lookups
_handle_id_cache: dict[str, int] | None = None


def _get_handle_to_id(client: ShopifyClient, cfg: dict, plan: SyncPlan) -> dict[str, int]:
    """Fetch and cache handle -> Shopify product ID mapping."""
    global _handle_id_cache
    if _handle_id_cache is None:
        live_products = client.get_all_products(vendor=cfg["vendor"])
        _handle_id_cache = {p["handle"]: p["id"] for p in live_products}
    return _handle_id_cache


def verify(client: ShopifyClient, cfg: dict) -> tuple[bool, dict]:
    """
    Verify that the Shopify store matches the catalogue.
    Returns (success, report_dict).
    """
    logger.info("Verification: fetching live products from Shopify...")
    live_products = client.get_all_products(vendor=cfg["vendor"])
    live_by_handle = {p["handle"]: p for p in live_products}

    logger.info("Verification: parsing catalogue...")
    catalogue = parse_shopify_csv(cfg["catalogue_csv"], vendor_filter=cfg["vendor"])

    missing = []
    image_mismatch = []
    verified_ok = []

    for handle, cat_prod in catalogue.items():
        if handle not in live_by_handle:
            missing.append({"title": cat_prod.title, "handle": handle})
            continue

        live = live_by_handle[handle]
        live_img_count = len(live.get("images", []))
        cat_img_count = len(cat_prod.images)
        if live_img_count != cat_img_count:
            image_mismatch.append({
                "handle": handle,
                "title": cat_prod.title,
                "shopify_images": live_img_count,
                "catalogue_images": cat_img_count,
            })
        else:
            verified_ok.append(handle)

    print("\n" + "=" * 60)
    print("  VERIFICATION REPORT")
    print("=" * 60)
    print(f"  Catalogue products:    {len(catalogue)}")
    print(f"  Live Shopify products: {len(live_products)}")
    print()

    if missing:
        print(f"  MISSING {len(missing)} products from Shopify:")
        for m in missing[:20]:
            print(f"      - {m['title']} ({m['handle']})")
        if len(missing) > 20:
            print(f"      ... and {len(missing) - 20} more")
    else:
        print("  All catalogue products exist in Shopify.")

    if image_mismatch:
        print(f"\n  IMAGE MISMATCH on {len(image_mismatch)} products:")
        for m in image_mismatch[:20]:
            print(f"      - {m['handle']}: Shopify={m['shopify_images']}, catalogue={m['catalogue_images']}")
        if len(image_mismatch) > 20:
            print(f"      ... and {len(image_mismatch) - 20} more")
    else:
        print("  Image counts match for all products.")

    print(f"\n  Verified OK: {len(verified_ok)}")
    print("=" * 60 + "\n")

    report = {
        "catalogue_count": len(catalogue),
        "live_count": len(live_products),
        "missing": missing,
        "image_mismatch": image_mismatch,
        "verified_ok_count": len(verified_ok),
    }

    ok = len(missing) == 0 and len(image_mismatch) == 0
    return ok, report


def write_report(report: dict, path: str = "sync_report.json"):
    """Write the sync report as JSON (useful as a GitHub Actions artifact)."""
    report["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info("Report written to %s", path)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync 'What You Need' vendor catalogue to Shopify.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="Execute changes against the live Shopify store.")
    mode.add_argument("--compare-only", action="store_true",
                      help="Only compare catalogues and print the diff.")
    mode.add_argument("--verify", action="store_true",
                      help="Verify Shopify matches the catalogue (read-only).")

    # Behaviour flags
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip interactive confirmation (for CI).")
    parser.add_argument("--from-api", action="store_true",
                        help="Fetch current Shopify state from API instead of CSV.")
    parser.add_argument("--no-publish", action="store_true",
                        help="Don't publish new products to Online Store.")
    parser.add_argument("--no-tags", action="store_true",
                        help="Don't auto-tag new products.")
    parser.add_argument("--no-prices", action="store_true",
                        help="Don't calculate/set retail prices from costs.")
    parser.add_argument("--report", default="sync_report.json",
                        help="Path for JSON report output (default: sync_report.json).")

    # Overrides
    parser.add_argument("--shopify-csv", help="Override path to Shopify export CSV.")
    parser.add_argument("--catalogue-csv", help="Override path to new catalogue CSV.")
    parser.add_argument("--vendor", help="Override vendor name filter.")

    args = parser.parse_args()
    cfg = load_config()

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
        client = None
        if args.from_api:
            client = ShopifyClient(cfg["store_url"], cfg["access_token"])
            client.test_connection()
        plan = build_plan(cfg, client=client, from_api=args.from_api)
        print_plan(plan)
        return

    # ------------------------------------------------------------------
    # Verify mode
    # ------------------------------------------------------------------
    if args.verify:
        client = ShopifyClient(cfg["store_url"], cfg["access_token"])
        client.test_connection()
        ok, report = verify(client, cfg)
        write_report({"verification": report}, args.report)
        sys.exit(0 if ok else 1)

    # ------------------------------------------------------------------
    # Sync mode (dry-run or live)
    # ------------------------------------------------------------------
    client = None
    if args.from_api or args.live:
        client = ShopifyClient(cfg["store_url"], cfg["access_token"])
        shop = client.test_connection()
        logger.info("Connected to: %s", shop["name"])

    plan = build_plan(cfg, client=client, from_api=args.from_api)
    print_plan(plan)

    total_actions = len(plan.to_create) + len(plan.to_update)
    if total_actions == 0:
        logger.info("Nothing to do -- store is already in sync.")
        write_report({"status": "already_in_sync"}, args.report)
        return

    if not args.live:
        print("This was a DRY RUN. No changes were made.")
        print("Re-run with --live to apply changes.\n")
        return

    # Live execution
    if not args.yes:
        print(f"\nAbout to make {total_actions} change(s) to {shop['name']}.")
        confirm = input("Type 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return

    # Reset cache before execution
    global _handle_id_cache
    _handle_id_cache = None

    report = execute_plan(
        client,
        plan,
        cfg,
        auto_tag=not args.no_tags,
        publish=not args.no_publish,
        set_prices=not args.no_prices,
    )

    # Post-sync verification
    print("\nRunning post-sync verification...")
    ok, verify_report = verify(client, cfg)
    report["verification"] = verify_report

    if ok:
        report["status"] = "success"
        logger.info("Sync complete -- all products verified.")
    else:
        report["status"] = "completed_with_discrepancies"
        logger.warning("Sync complete but verification found discrepancies.")

    write_report(report, args.report)

    # Exit with error code if verification failed (useful for CI)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
