# WYN → Shopify Product Sync

Syncs the "What You Need" vendor catalogue with a Shopify store. Compares a
new catalogue CSV against your current Shopify product export to determine
what needs to be created, what needs updated images, and what is unchanged —
then executes those changes via the Shopify Admin API.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Shopify credentials
```

### Shopify API credentials

1. In Shopify Admin, go to **Settings → Apps and sales channels → Develop apps**
2. Create a custom app with these **Admin API scopes**:
   - `read_products`
   - `write_products`
3. Install the app and copy the Admin API access token into `.env`

### Data files

Place two CSVs in the `data/` directory:

| File | Description |
|------|-------------|
| `data/shopify_export.csv` | Current Shopify product export (Admin → Products → Export) |
| `data/wyn_catalogue.csv` | New WYN catalogue (same Shopify CSV format) |

## Usage

### 1. Compare catalogues (no API calls)

```bash
python sync.py --compare-only
```

Shows a report of what would be created, updated, or left alone.

### 2. Dry run (default)

```bash
python sync.py
```

Parses both CSVs and prints the plan, but makes no changes.

### 3. Live sync

```bash
python sync.py --live
```

Executes the sync: creates new products, replaces images on updated products,
then runs a verification pass.

### 4. Verify

```bash
python sync.py --verify
```

Checks that every product in the catalogue exists in Shopify with the correct
image count. Read-only API calls.

### CLI overrides

```
--shopify-csv PATH    Override the Shopify export CSV path
--catalogue-csv PATH  Override the new catalogue CSV path
--vendor NAME         Override the vendor name filter
```

## How it works

```
┌─────────────────┐     ┌─────────────────┐
│ shopify_export   │     │ wyn_catalogue    │
│    .csv          │     │    .csv          │
└────────┬────────┘     └────────┬────────┘
         │                       │
         └───────┐   ┌──────────┘
                 ▼   ▼
          ┌──────────────┐
          │  Compare by  │
          │   handle     │
          └──────┬───────┘
                 │
        ┌────────┼────────┐
        ▼        ▼        ▼
   New items  Changed   Unchanged
              images
        │        │
        ▼        ▼
   POST create  DELETE old images
   /products    POST new images
        │        │
        └────┬───┘
             ▼
       Verify all products
       match catalogue
```

**Matching**: Products are matched by their `Handle` (the URL slug). This is
the most reliable identifier across Shopify exports.

**Image comparison**: Image URLs are compared after stripping query parameters
(Shopify CDN adds cache-busters like `?v=123456`). If the base URLs differ,
images are replaced.

**Safety**: Dry-run is the default. Live mode requires `--live` flag and an
interactive confirmation prompt before making any changes.

## Logs

Every run writes a timestamped log to `logs/`.
