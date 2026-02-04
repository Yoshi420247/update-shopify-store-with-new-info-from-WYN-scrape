# WYN -> Shopify Product Sync

Syncs the "What You Need" vendor catalogue with a Shopify store. Compares the
new catalogue against current Shopify products (via API or CSV export), then
creates missing products, replaces updated images, syncs prices/costs, auto-tags
for collection sorting, and publishes to the Online Store.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Shopify credentials
```

### Shopify API credentials

1. In Shopify Admin, go to **Settings > Apps and sales channels > Develop apps**
2. Create a custom app with these **Admin API scopes**:
   - `read_products`, `write_products`
   - `read_inventory`, `write_inventory`
   - `read_publications`, `write_publications`
3. Install the app and copy the Admin API access token into `.env`

### Data files

Place the new WYN catalogue CSV in `data/wyn_catalogue.csv` (standard Shopify
product export format).

If using `--from-api` (recommended), no Shopify export CSV is needed — the
tool fetches current state directly from the Shopify Admin API.

## Usage

### Compare (no changes made)

```bash
# Using live API data (recommended):
python sync.py --from-api --compare-only

# Using a Shopify export CSV:
python sync.py --compare-only
```

### Dry run (default)

```bash
python sync.py --from-api
```

Prints the sync plan but makes no changes.

### Live sync

```bash
python sync.py --from-api --live

# Skip interactive confirmation (for CI/automation):
python sync.py --from-api --live --yes
```

Creates new products, replaces images, updates prices/costs, auto-tags, and
publishes to the Online Store. Then runs a verification pass.

### Verify

```bash
python sync.py --verify
```

Checks that every catalogue product exists in Shopify with correct image counts.

### Feature flags

```
--no-tags       Don't auto-tag new products
--no-publish    Don't publish new products to Online Store
--no-prices     Don't calculate retail prices from costs
--report PATH   Custom path for JSON report output
```

## GitHub Actions

The workflow **"Sync WYN Products to Shopify"** can be triggered from the
Actions tab with these inputs:

| Input | Options | Default |
|-------|---------|---------|
| action | dry-run, live-sync, compare-only, verify | dry-run |
| source | from-api, from-csv | from-api |
| catalogue_csv | path to CSV in repo | data/wyn_catalogue.csv |
| auto_tag | true/false | true |
| publish | true/false | true |
| set_prices | true/false | true |

**Required secrets**: `SHOPIFY_STORE`, `SHOPIFY_ACCESS_TOKEN`

Artifacts uploaded after each run: `sync_report.json` and `logs/`.

## What it does

```
                    ┌─────────────┐
                    │ Shopify API │  (--from-api)
                    │  or CSV     │  (default)
                    └──────┬──────┘
                           │
  ┌──────────────┐         │
  │ WYN catalogue│─────────┤
  │    .csv      │         │
  └──────────────┘    Compare by
                       handle
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         New items    Changed      Unchanged
                    (img/price/cost)
              │            │
              ▼            ▼
         Create via    Update images
         POST API      Update prices
         Auto-tag      Update costs
         Set 2x price
              │            │
              └─────┬──────┘
                    ▼
           Publish to Online Store
                    │
                    ▼
            Verify all products
            Write JSON report
```

## Auto-tagging

New products are automatically tagged with the Oil Slick taxonomy based on
keyword matching in the title:

| Namespace | Example tags |
|-----------|-------------|
| `family:` | `glass-bong`, `grinder`, `dab-tool` |
| `pillar:` | `smokeshop-device`, `accessory` |
| `use:` | `flower-smoking`, `dabbing`, `rolling` |
| `material:` | `glass`, `silicone`, `quartz` |
| `brand:` | `raw`, `cookies`, `zig-zag` |
| `style:` | `animal`, `character`, `halloween` |

These tags drive Shopify smart-collection auto-sorting.

## Pricing

New products without a price get retail = cost x 2 (standard Oil Slick markup).
