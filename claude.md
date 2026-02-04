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
```

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
- **Pricing** follows the 2x cost markup rule established across the Oil Slick
  tooling.  Retail = cost * 2.
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
