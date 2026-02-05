# WYN -> Shopify Product Sync

## Project purpose

Syncs the "What You Need" (WYN) vendor catalogue with the Oil Slick Shopify
store (`oilslickpad.com`).  WYN is a wholesale supplier of ~750+ smokeshop
products (bongs, dab rigs, pipes, grinders, rolling papers, etc.).  When WYN
updates their catalogue with new products or new images, this tool:

1. Compares the new catalogue against live Shopify state
2. Creates missing products with auto-generated taxonomy tags
3. Replaces images on products where WYN has updated photos
4. Syncs prices (2x cost markup) and wholesale costs
5. Publishes new products to the Online Store sales channel
6. Runs a verification pass and outputs a JSON report

## Architecture

```
sync.py           Main CLI orchestrator (dry-run / live / verify / compare-only)
shopify_api.py    Shopify Admin REST + GraphQL client (API version 2025-01)
catalogue.py      CSV parser, API-to-internal converter, comparison engine
tags.py           Auto-tagging engine (Oil Slick taxonomy from collection strategy)
supabase_log.py   Supabase audit logging + product state snapshots (optional)
```

### Execution phases (live sync)

1. **Create new products** -- auto-tags, 2x pricing, `inventory_management: "shopify"`
2. **Set costs on new products** -- via `PUT /inventory_items/{id}.json` (Shopify ignores cost during creation)
3. **Update images** -- upload new images first, then delete old ones (safe replacement)
4. **Update prices** -- sync price and compare_at_price per variant SKU
5. **Update costs** -- sync wholesale cost on existing products via Inventory Items API; recalculates retail only when cost actually differs
6. **Add new variants** -- add variants with new SKUs to existing products, then set their costs
7. **Publish to Online Store** -- via GraphQL `publishablePublish` mutation
8. **Save Supabase snapshots** -- store current product state for faster future diffs

### Key design decisions

- **`--from-api` mode** eliminates the need for manual CSV exports.  The tool
  fetches current Shopify state directly via the Admin API, so it can run
  fully automated in CI.
- **Dry-run is the default.**  `--live` must be explicitly passed to make
  changes.  In CI, `--yes` skips the interactive confirmation.
- **Products matched by `handle`** (the URL slug) -- the most reliable
  identifier across Shopify exports and API responses.
- **Image comparison** strips CDN query params (`?v=123456`) before comparing
  URLs.  If the base URL set differs, all images are replaced.
- **Safe image replacement** -- new images are uploaded before old ones are
  deleted, so the product always has images visible on the storefront.
- **Auto-tagging** uses regex keyword matching on product titles to assign
  `family:`, `pillar:`, `use:`, `material:`, `brand:`, `style:`, `joint_size:`,
  and `joint_gender:` tags.  These tags drive smart-collection auto-sorting.
- **Pricing** follows the 2x cost markup rule: Shopify retail price = cost x 2,
  always.  The CSV "Variant Price" column is never used as-is -- retail is always
  recalculated from cost.  When costs change on existing products, the retail
  price is also recalculated (only if it actually differs).
- **WYN CSV column swap** -- the WYN catalogue has inverted columns: its
  "Variant Price" is the wholesale cost and "Cost per item" is the retail.
  The parser swaps these with `swap_price_cost=True` so internal fields are
  correct (cost = wholesale, price = retail from cost x 2).
- **Rate limiting** is handled with a minimum 0.55s interval between requests
  plus automatic retry on 429 responses.
- **Request timeouts** -- all HTTP requests have a 30-second timeout to prevent
  indefinite hangs on network issues.
- **CSV validation** -- the tool validates that the catalogue CSV exists before
  attempting any operations, providing a clear error message.
- **Duplicate SKU detection** -- warns if the same SKU appears multiple times
  in a single product during CSV parsing.

## Running

### Local

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
# Place new catalogue at data/wyn_catalogue.csv.csv

python sync.py --from-api                    # dry run
python sync.py --from-api --live --yes       # live, no prompt
python sync.py --verify                      # check store matches catalogue
```

### GitHub Actions

Trigger the **"Sync WYN Products to Shopify"** workflow from the Actions tab.
Secrets required: `SHOPIFY_STORE`, `SHOPIFY_ACCESS_TOKEN`.
Optional secrets for audit logging: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.

Inputs:
- **action**: dry-run | live-sync | compare-only | verify
- **source**: from-api (recommended) | from-csv
- **catalogue_csv**: path to new catalogue CSV in the repo (default: `data/wyn_catalogue.csv.csv`)
- **auto_tag / publish / set_prices**: boolean toggles

Artifacts uploaded: `sync_report.json` and `logs/`.

## Tag taxonomy

Tags are namespaced (`namespace:value`) and drive smart-collection membership:

| Namespace | Purpose | Example |
|-----------|---------|---------|
| `family:` | Product family | `family:glass-bong`, `family:grinder` |
| `pillar:` | Business category | `pillar:smokeshop-device`, `pillar:accessory` |
| `use:` | Use case | `use:flower-smoking`, `use:dabbing` |
| `material:` | Material | `material:glass`, `material:silicone` |
| `brand:` | Brand name | `brand:raw`, `brand:cookies` |
| `style:` | Feature/theme | `style:animal`, `style:halloween` |
| `joint_size:` | Joint size | `joint_size:14mm` |
| `joint_gender:` | Joint gender | `joint_gender:female` |

Rules are defined in `tags.py` as compiled regex patterns.  First match wins
for single-value namespaces (family, pillar, use, brand).  Multiple matches
allowed for material and style.

## Related repositories

- `Yoshi420247/Shopify-Collection-strategy-and-menu-creation` -- collection
  structure, smart-collection rules, menu setup, tag taxonomy source
- `Yoshi420247/makeAIproductdescription` -- AI product description generator
  (can be run after this sync to generate descriptions for new products)

## Supabase integration (optional)

If `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are set, the tool logs every sync
run and per-product action to Supabase for audit trail and state tracking.

### Setup

1. Run `python sync.py --setup-supabase` to print the table-creation SQL.
2. Paste the SQL into the Supabase SQL Editor and run it.
3. Add `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` to `.env` (or GitHub Actions secrets).

### Tables

| Table | Purpose |
|-------|---------|
| `sync_runs` | One row per sync invocation (status, timestamps, summary) |
| `sync_actions` | One row per product-level action (create, update_images, error, etc.) |
| `product_snapshots` | Latest known state of each product (handle, images, prices) |

If Supabase is not configured, `SupabaseLogger` methods silently no-op and
the tool works exactly the same without any logging.

## File layout

```
.
├── .github/workflows/
│   └── sync-wyn-products.yml    GitHub Actions workflow
├── data/
│   ├── .gitkeep
│   ├── shopify_export.csv       (gitignored) current Shopify export
│   └── wyn_catalogue.csv.csv   (gitignored) new WYN catalogue
├── logs/                        (gitignored) timestamped sync logs
├── .env.example                 Template for credentials
├── .gitignore
├── catalogue.py                 CSV parser + comparison engine
├── claude.md                    This file
├── README.md                    User-facing documentation
├── requirements.txt             Python dependencies
├── shopify_api.py               Shopify REST + GraphQL client
├── supabase_log.py              Supabase audit logger (optional)
├── sync.py                      Main entry point
└── tags.py                      Auto-tagging engine
```

## Shopify API scopes required

- `read_products`
- `write_products`
- `read_inventory`
- `write_inventory`
- `read_publications` (for publishing via GraphQL)
- `write_publications`

## Known issues and past fixes

### Issues fixed in codebase review (2025-02-05)

1. **No request timeouts** -- `shopify_api.py` made HTTP requests without any
   timeout, meaning a stalled connection could hang the process forever.
   Fixed: added 30-second timeout to all REST and GraphQL requests.

2. **Unsafe image replacement** -- `replace_product_images()` deleted all
   existing images before uploading new ones. If an upload failed mid-way,
   the product would be left with zero images on the storefront.
   Fixed: new images are now uploaded first, then old images are deleted,
   then positions are corrected.

3. **Outdated Shopify API version** -- was using `2024-01` which Shopify
   deprecates after ~1 year. Updated to `2025-01`.

4. **Variant condition bug** -- `catalogue.py` used `csv_price` (pre-swap
   variable) instead of `cost` (post-swap) in the condition that decides
   whether a CSV row has variant data. This meant rows with only a cost
   value but no SKU and no csv_price would be silently dropped when
   `swap_price_cost=True`. Fixed to use `cost` (the post-swap value).

5. **Silent row skipping** -- CSV rows with no handle were silently skipped.
   Fixed: a warning is now logged with the count of skipped rows.

6. **No duplicate SKU detection** -- if the WYN CSV had duplicate SKUs
   within the same product, both would be added and cause API errors.
   Fixed: parser now logs a warning for each duplicate SKU found.

7. **Missing CSV validation** -- if the catalogue CSV path was wrong, the
   tool would crash with an unhelpful FileNotFoundError deep in the stack.
   Fixed: early validation in `main()` with a clear error message and
   suggestion to use `--catalogue-csv`.

8. **No final summary log** -- after a live sync, there was no single log
   line summarizing what happened. Fixed: a final `logger.info()` line
   now reports created/updated/error counts.

9. **Global mutable cache** -- `_handle_id_cache` was a module-level global
   dict, which is fragile and makes testing difficult.  Refactored to use
   a local cache dict passed as a parameter within `execute_plan()`.

10. **Redundant price updates in Phase 5** -- when costs changed, the retail
    price was recalculated and updated via API even if it already matched.
    Fixed: now compares the current retail price to the calculated one and
    skips the API call if they're the same.

11. **Path inconsistencies** -- `.env.example` referenced `data/wyn_catalogue.csv`
    but the actual uploaded file and `sync.py` default used
    `data/wyn_catalogue.csv.csv`.  README had the same mismatch.
    Fixed: all references now consistently use `data/wyn_catalogue.csv.csv`.

### Known limitations (not yet addressed)

- **No inventory level initialization** -- new products are created with 0
  inventory. Products appear out-of-stock until inventory is manually set.
  The API methods `set_inventory_level()` and `get_locations()` exist in
  `shopify_api.py` but are not yet wired into the sync flow.

- **No variant removal** -- the diff engine detects removed SKUs but the
  sync doesn't delete them from Shopify. This is intentional (conservative)
  but could be added behind a flag.

- **WYN CSV column swap is not auto-detected** -- the swap is hardcoded
  via `swap_price_cost=True`. If WYN changes their export format, prices
  would be inverted with no warning. A heuristic check could be added.

- **Double `.csv.csv` extension** -- the WYN catalogue file was uploaded
  with a double extension (`wyn_catalogue.csv.csv`). This works but looks
  odd. When a new catalogue is uploaded, consider naming it with a single
  `.csv` extension and updating the default path.
