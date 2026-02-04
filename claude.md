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
shopify_api.py    Shopify Admin REST + GraphQL client
catalogue.py      CSV parser, API-to-internal converter, comparison engine
tags.py           Auto-tagging engine (Oil Slick taxonomy from collection strategy)
supabase_log.py   Supabase audit logging + product state snapshots (optional)
```

### Execution phases (live sync)

1. **Create new products** — auto-tags, 2x pricing, `inventory_management: "shopify"`
2. **Set costs on new products** — via `PUT /inventory_items/{id}.json` (Shopify ignores cost during creation)
3. **Update images** — delete existing + re-upload if catalogue has different image URLs
4. **Update prices** — sync price and compare_at_price per variant SKU
5. **Update costs** — sync wholesale cost on existing products via Inventory Items API
6. **Add new variants** — add variants with new SKUs to existing products, then set their costs
7. **Publish to Online Store** — via GraphQL `publishablePublish` mutation
8. **Save Supabase snapshots** — store current product state for faster future diffs

### Key design decisions

- **`--from-api` mode** eliminates the need for manual CSV exports.  The tool
  fetches current Shopify state directly via the Admin API, so it can run
  fully automated in CI.
- **Dry-run is the default.**  `--live` must be explicitly passed to make
  changes.  In CI, `--yes` skips the interactive confirmation.
- **Products matched by `handle`** (the URL slug) — the most reliable
  identifier across Shopify exports and API responses.
- **Image comparison** strips CDN query params (`?v=123456`) before comparing
  URLs.  If the base URL set differs, all images are replaced.
- **Auto-tagging** uses regex keyword matching on product titles to assign
  `family:`, `pillar:`, `use:`, `material:`, `brand:`, `style:`, `joint_size:`,
  and `joint_gender:` tags.  These tags drive smart-collection auto-sorting.
- **Pricing** follows the 2x cost markup rule: Shopify retail price = cost x 2,
  always.  The CSV "Variant Price" column is never used as-is — retail is always
  recalculated from cost.  When costs change on existing products, the retail
  price is also recalculated.
- **WYN CSV column swap** — the WYN catalogue has inverted columns: its
  "Variant Price" is the wholesale cost and "Cost per item" is the retail.
  The parser swaps these with `swap_price_cost=True` so internal fields are
  correct (cost = wholesale, price = retail from cost x 2).
- **Rate limiting** is handled with a minimum 0.55s interval between requests
  plus automatic retry on 429 responses.

## Running

### Local

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
# Place new catalogue at data/wyn_catalogue.csv

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
- **catalogue_csv**: path to new catalogue CSV in the repo
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

- `Yoshi420247/Shopify-Collection-strategy-and-menu-creation` — collection
  structure, smart-collection rules, menu setup, tag taxonomy source
- `Yoshi420247/makeAIproductdescription` — AI product description generator
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
│   └── wyn_catalogue.csv        (gitignored) new WYN catalogue
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
