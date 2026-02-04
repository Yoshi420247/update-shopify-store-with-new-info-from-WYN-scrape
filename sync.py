#!/usr/bin/env python3
"""
WYN -> Shopify Product Sync

Compares a new "What You Need" vendor catalogue (Shopify-format CSV) against
the products currently in a Shopify store, then:

  1. Creates new products (with auto-tags, 2x pricing, inventory tracking).
  2. Sets costs on newly created products via the Inventory Items API.
  3. Updates images on existing products where the catalogue has new images.
  4. Syncs prices and compare-at-prices where they differ.
  5. Syncs costs on existing products.
  6. Adds new variants to existing products.
  7. Publishes new products to the Online Store sales channel.
  8. Runs a verification pass and writes a JSON report.
  9. Logs everything to Supabase (if configured) for audit trail.

Usage:
    python sync.py --from-api                    # dry run
    python sync.py --from-api --live --yes       # live, CI mode
    python sync.py --from-api --compare-only     # diff only
    python sync.py --verify                      # check store vs catalogue
    python sync.py --setup-supabase              # print SQL for table setup
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
    variant_to_shopify_payload,
)
from shopify_api import ShopifyClient, ShopifyAPIError
from supabase_log import SupabaseLogger, print_setup_sql
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
    if from_api and client:
        logger.info("Fetching current products from Shopify API (vendor=%s)...", cfg["vendor"])
        api_products = client.get_all_products(vendor=cfg["vendor"])
        existing = products_from_api(api_products)
    else:
        logger.info("Parsing Shopify export CSV: %s", cfg["shopify_csv"])
        existing = parse_shopify_csv(cfg["shopify_csv"], vendor_filter=cfg["vendor"])

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
            if diff.added_skus:
                print(f"      new SKUs: {', '.join(diff.added_skus)}")
            if diff.removed_skus:
                print(f"      removed SKUs: {', '.join(diff.removed_skus)}")
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
    sb: SupabaseLogger = None,
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
        "costs_set_on_new": [],
        "images_updated": [],
        "prices_updated": [],
        "costs_updated": [],
        "variants_added": [],
        "published": [],
        "errors": [],
    }
    sb = sb or SupabaseLogger()  # no-op if not configured

    # ======= Phase 1: Create new products =======
    created_products = []  # list of (catalogue_product, shopify_response)
    if plan.to_create:
        logger.info("Phase 1: Creating %d new products...", len(plan.to_create))
        for i, product in enumerate(plan.to_create, 1):
            logger.info("  [%d/%d] Creating: %s", i, len(plan.to_create), product.title)

            if auto_tag:
                product.tags = generate_tags(product.title, product.tags, product.product_type)
                logger.info("    Tags: %s", product.tags)

            if set_prices:
                for v in product.variants:
                    if v.cost and (not v.price or v.price == "0.00"):
                        retail = calculate_retail_price(v.cost)
                        if retail:
                            v.price = retail
                            logger.info("    Price for SKU %s: cost=%s -> retail=%s",
                                       v.sku, v.cost, retail)

            payload = product_to_shopify_payload(product)
            try:
                created = client.create_product(payload)
                created_products.append((product, created))
                report["created"].append({
                    "title": product.title,
                    "handle": product.handle,
                    "shopify_id": created["id"],
                    "variants": len(created.get("variants", [])),
                    "images": len(created.get("images", [])),
                })
                sb.log_action("create", product.handle, product.title,
                              shopify_id=created["id"],
                              details={"variants": len(created.get("variants", []))})
                logger.info("    Created (id=%s)", created["id"])
            except ShopifyAPIError as e:
                msg = f"Failed to create {product.title}: {e}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
                sb.log_error(product.handle, product.title, str(e))
    else:
        logger.info("Phase 1: No new products to create.")

    # ======= Phase 2: Set costs on newly created products =======
    # Shopify ignores 'cost' on the variant object during creation.
    # We must set it via PUT /inventory_items/{id}.json afterwards.
    if created_products:
        logger.info("Phase 2: Setting costs on %d newly created products...", len(created_products))
        for cat_prod, shopify_prod in created_products:
            cost_map = {v.sku: v.cost for v in cat_prod.variants if v.sku and v.cost}
            if cost_map:
                try:
                    client.set_costs_on_product(shopify_prod, cost_map)
                    report["costs_set_on_new"].append({
                        "handle": cat_prod.handle,
                        "skus": list(cost_map.keys()),
                    })
                except ShopifyAPIError as e:
                    msg = f"Failed to set costs on {cat_prod.title}: {e}"
                    logger.error("    %s", msg)
                    report["errors"].append(msg)
    else:
        logger.info("Phase 2: No costs to set on new products.")

    # ======= Phase 3: Update images =======
    image_updates = plan.to_update_images
    if image_updates:
        logger.info("Phase 3: Updating images on %d products...", len(image_updates))
        handle_to_id = _get_handle_to_id(client, cfg)

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
                sb.log_action("update_images", old_prod.handle, old_prod.title,
                              shopify_id=product_id,
                              details={"old": len(old_prod.images), "new": len(new_prod.images)})
                logger.info("    Replaced %d -> %d images",
                           len(old_prod.images), len(new_prod.images))
            except ShopifyAPIError as e:
                msg = f"Failed to update images for {old_prod.title}: {e}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
                sb.log_error(old_prod.handle, old_prod.title, str(e))
    else:
        logger.info("Phase 3: No image updates needed.")

    # ======= Phase 4: Update prices =======
    price_updates = plan.to_update_prices
    if price_updates:
        logger.info("Phase 4: Updating prices on %d products...", len(price_updates))
        handle_to_id = _get_handle_to_id(client, cfg)

        for i, (old_prod, new_prod, diff) in enumerate(price_updates, 1):
            logger.info("  [%d/%d] Updating prices: %s", i, len(price_updates), old_prod.title)
            product_id = old_prod.shopify_id or handle_to_id.get(old_prod.handle)
            if not product_id:
                continue
            try:
                live_product = client.get_product(product_id)
                live_variants = {v["sku"]: v for v in live_product.get("variants", []) if v.get("sku")}
                new_variant_map = {v.sku: v for v in new_prod.variants if v.sku}

                for sku, new_v in new_variant_map.items():
                    live_v = live_variants.get(sku)
                    if not live_v:
                        continue
                    updates = {}
                    if new_v.price:
                        updates["price"] = new_v.price
                    if new_v.compare_at_price:
                        updates["compare_at_price"] = new_v.compare_at_price
                    if updates:
                        client.update_variant(live_v["id"], updates)
                        logger.info("    Updated SKU %s: %s", sku, updates)

                report["prices_updated"].append({
                    "title": old_prod.title, "handle": old_prod.handle,
                })
                sb.log_action("update_prices", old_prod.handle, old_prod.title,
                              shopify_id=product_id)
            except ShopifyAPIError as e:
                msg = f"Failed to update prices for {old_prod.title}: {e}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
    else:
        logger.info("Phase 4: No price updates needed.")

    # ======= Phase 5: Update costs =======
    cost_updates = plan.to_update_costs
    if cost_updates:
        logger.info("Phase 5: Updating costs on %d products...", len(cost_updates))
        handle_to_id = _get_handle_to_id(client, cfg)

        for i, (old_prod, new_prod, diff) in enumerate(cost_updates, 1):
            logger.info("  [%d/%d] Updating costs: %s", i, len(cost_updates), old_prod.title)
            product_id = old_prod.shopify_id or handle_to_id.get(old_prod.handle)
            if not product_id:
                continue
            try:
                live_product = client.get_product(product_id)
                cost_map = {v.sku: v.cost for v in new_prod.variants if v.sku and v.cost}
                client.set_costs_on_product(live_product, cost_map)
                report["costs_updated"].append({
                    "title": old_prod.title, "handle": old_prod.handle,
                })
                sb.log_action("update_costs", old_prod.handle, old_prod.title,
                              shopify_id=product_id)
            except ShopifyAPIError as e:
                msg = f"Failed to update costs for {old_prod.title}: {e}"
                logger.error("    %s", msg)
                report["errors"].append(msg)
    else:
        logger.info("Phase 5: No cost updates needed.")

    # ======= Phase 6: Add new variants to existing products =======
    variant_additions = plan.to_add_variants
    if variant_additions:
        logger.info("Phase 6: Adding variants to %d products...", len(variant_additions))
        handle_to_id = _get_handle_to_id(client, cfg)

        for i, (old_prod, new_prod, diff) in enumerate(variant_additions, 1):
            logger.info("  [%d/%d] Adding variants to: %s (SKUs: %s)",
                       i, len(variant_additions), old_prod.title,
                       ", ".join(diff.added_skus))
            product_id = old_prod.shopify_id or handle_to_id.get(old_prod.handle)
            if not product_id:
                continue

            new_variant_map = {v.sku: v for v in new_prod.variants if v.sku}
            added_count = 0
            for sku in diff.added_skus:
                variant = new_variant_map.get(sku)
                if not variant:
                    continue

                if set_prices and variant.cost and (not variant.price or variant.price == "0.00"):
                    retail = calculate_retail_price(variant.cost)
                    if retail:
                        variant.price = retail

                try:
                    payload = variant_to_shopify_payload(variant)
                    created_v = client.create_variant(product_id, payload)

                    # Set cost on the new variant's inventory item
                    if variant.cost and created_v.get("inventory_item_id"):
                        client.update_inventory_item_cost(
                            created_v["inventory_item_id"], variant.cost)

                    added_count += 1
                    logger.info("    Added variant SKU=%s (id=%s)", sku, created_v["id"])
                except ShopifyAPIError as e:
                    msg = f"Failed to add variant {sku} to {old_prod.title}: {e}"
                    logger.error("    %s", msg)
                    report["errors"].append(msg)

            if added_count:
                report["variants_added"].append({
                    "title": old_prod.title,
                    "handle": old_prod.handle,
                    "skus_added": diff.added_skus,
                    "count": added_count,
                })
                sb.log_action("add_variants", old_prod.handle, old_prod.title,
                              shopify_id=product_id,
                              details={"skus": diff.added_skus})
    else:
        logger.info("Phase 6: No variants to add.")

    # ======= Phase 7: Publish new products =======
    created_ids = [c["shopify_id"] for c in report["created"]]
    if publish and created_ids:
        logger.info("Phase 7: Publishing %d new products to Online Store...", len(created_ids))
        try:
            count = client.publish_products_to_online_store(created_ids)
            report["published"] = [{"count": count, "total": len(created_ids)}]
            logger.info("    Published %d/%d products", count, len(created_ids))
        except ShopifyAPIError as e:
            msg = f"Failed to publish products: {e}"
            logger.error("    %s", msg)
            report["errors"].append(msg)
    else:
        logger.info("Phase 7: No products to publish.")

    # ======= Phase 8: Save snapshots to Supabase =======
    if sb.enabled and (report["created"] or report["images_updated"]):
        logger.info("Phase 8: Saving product snapshots to Supabase...")
        live_products = client.get_all_products(vendor=cfg["vendor"])
        for lp in live_products:
            sb.save_snapshot(
                handle=lp["handle"],
                title=lp.get("title", ""),
                shopify_id=lp["id"],
                image_urls=[img["src"] for img in lp.get("images", [])],
                variant_skus=[v["sku"] for v in lp.get("variants", []) if v.get("sku")],
                prices={v["sku"]: v["price"] for v in lp.get("variants", []) if v.get("sku")},
            )

    return report


# Cache for handle -> ID lookups
_handle_id_cache: dict[str, int] | None = None


def _get_handle_to_id(client: ShopifyClient, cfg: dict) -> dict[str, int]:
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
    variant_mismatch = []
    verified_ok = []

    for handle, cat_prod in catalogue.items():
        if handle not in live_by_handle:
            missing.append({"title": cat_prod.title, "handle": handle})
            continue

        live = live_by_handle[handle]
        ok = True

        # Image count check
        live_img_count = len(live.get("images", []))
        cat_img_count = len(cat_prod.images)
        if live_img_count != cat_img_count:
            image_mismatch.append({
                "handle": handle, "title": cat_prod.title,
                "shopify_images": live_img_count, "catalogue_images": cat_img_count,
            })
            ok = False

        # Variant count check
        live_sku_count = len([v for v in live.get("variants", []) if v.get("sku")])
        cat_sku_count = len([v for v in cat_prod.variants if v.sku])
        if live_sku_count != cat_sku_count:
            variant_mismatch.append({
                "handle": handle, "title": cat_prod.title,
                "shopify_variants": live_sku_count, "catalogue_variants": cat_sku_count,
            })
            ok = False

        if ok:
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
    else:
        print("  Image counts match for all products.")

    if variant_mismatch:
        print(f"\n  VARIANT MISMATCH on {len(variant_mismatch)} products:")
        for m in variant_mismatch[:20]:
            print(f"      - {m['handle']}: Shopify={m['shopify_variants']}, catalogue={m['catalogue_variants']}")
    else:
        print("  Variant counts match for all products.")

    print(f"\n  Verified OK: {len(verified_ok)}")
    print("=" * 60 + "\n")

    report = {
        "catalogue_count": len(catalogue),
        "live_count": len(live_products),
        "missing": missing,
        "image_mismatch": image_mismatch,
        "variant_mismatch": variant_mismatch,
        "verified_ok_count": len(verified_ok),
    }

    ok = len(missing) == 0 and len(image_mismatch) == 0 and len(variant_mismatch) == 0
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
    mode.add_argument("--setup-supabase", action="store_true",
                      help="Print SQL to create Supabase tables, then exit.")

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

    # ------------------------------------------------------------------
    # Setup mode
    # ------------------------------------------------------------------
    if args.setup_supabase:
        print_setup_sql()
        return

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

    # Initialize Supabase logger
    sb = SupabaseLogger()
    sb.start_run(
        action="live-sync",
        vendor=cfg["vendor"],
        catalogue_path=cfg["catalogue_csv"],
    )

    report = execute_plan(
        client, plan, cfg, sb=sb,
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
    sb.finish_run(report["status"], report)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
