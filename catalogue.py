"""
Shopify CSV catalogue parser and comparison engine.

Parses standard Shopify product-export CSVs (where one product can span
multiple rows for variants and images), converts live API product dicts
into the same format, and compares two catalogues to determine what needs
to be created, updated, or left alone.
"""

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------------

@dataclass
class Variant:
    sku: str = ""
    price: str = ""
    compare_at_price: str = ""
    cost: str = ""
    option1_name: str = ""
    option1_value: str = ""
    option2_name: str = ""
    option2_value: str = ""
    option3_name: str = ""
    option3_value: str = ""
    grams: str = ""
    weight_unit: str = ""
    inventory_qty: str = ""
    inventory_policy: str = ""
    barcode: str = ""
    requires_shipping: str = ""
    taxable: str = ""
    variant_image: str = ""
    # Set when loaded from API (needed for updates)
    shopify_variant_id: int | None = None
    shopify_inventory_item_id: int | None = None


@dataclass
class Product:
    handle: str = ""
    title: str = ""
    body_html: str = ""
    vendor: str = ""
    product_type: str = ""
    tags: str = ""
    published: str = ""
    status: str = ""
    images: list[str] = field(default_factory=list)
    image_alts: list[str] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)
    # Set when loaded from API
    shopify_id: int | None = None
    _raw_rows: list[dict] = field(default_factory=list, repr=False)


# ----------------------------------------------------------------------
# CSV parsing
# ----------------------------------------------------------------------

def _normalise_key(col: str) -> str:
    """Lowercase, strip whitespace, collapse spaces."""
    return " ".join(col.strip().lower().split())


def parse_shopify_csv(path: str | Path, vendor_filter: str = None,
                      swap_price_cost: bool = False) -> dict[str, Product]:
    """
    Parse a Shopify product-export CSV into a dict keyed by handle.

    In the Shopify CSV format:
    - The first row for a product carries the Title, Handle, Body, etc.
    - Subsequent rows for the same product have blank Title/Handle and
      carry additional variants and/or images.

    If swap_price_cost is True, the "Variant Price" column is treated as
    the wholesale cost and "Cost per item" is treated as the retail price.
    This is needed for WYN catalogues where the columns are inverted
    relative to Shopify's convention.

    Returns {handle: Product}.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    products: dict[str, Product] = {}
    current_handle: str | None = None
    skipped_rows = 0

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        field_map = {_normalise_key(c): c for c in reader.fieldnames} if reader.fieldnames else {}

        def g(row: dict, normalised_name: str) -> str:
            original = field_map.get(normalised_name, "")
            return (row.get(original) or "").strip()

        for row in reader:
            handle = g(row, "handle")

            if not handle:
                handle = current_handle
            if not handle:
                skipped_rows += 1
                continue

            current_handle = handle

            row_vendor = g(row, "vendor")
            if vendor_filter and handle not in products and row_vendor:
                if row_vendor.lower() != vendor_filter.lower():
                    continue

            if handle not in products:
                products[handle] = Product(
                    handle=handle,
                    title=g(row, "title"),
                    body_html=g(row, "body (html)") or g(row, "body html"),
                    vendor=g(row, "vendor"),
                    product_type=g(row, "type") or g(row, "product type"),
                    tags=g(row, "tags"),
                    published=g(row, "published"),
                    status=g(row, "status"),
                )

            prod = products[handle]
            prod._raw_rows.append(row)

            img = g(row, "image src")
            if img and img not in prod.images:
                prod.images.append(img)
                prod.image_alts.append(g(row, "image alt text"))

            sku = g(row, "variant sku")
            csv_price = g(row, "variant price")
            csv_cost = g(row, "cost per item")

            # WYN catalogues have columns inverted: "Variant Price" is
            # actually wholesale cost, "Cost per item" is the retail price.
            if swap_price_cost:
                price = csv_cost   # retail price
                cost = csv_price   # wholesale cost
            else:
                price = csv_price
                cost = csv_cost

            if sku or price or cost:
                prod.variants.append(
                    Variant(
                        sku=sku,
                        price=price,
                        compare_at_price=g(row, "variant compare at price"),
                        cost=cost,
                        option1_name=g(row, "option1 name"),
                        option1_value=g(row, "option1 value"),
                        option2_name=g(row, "option2 name"),
                        option2_value=g(row, "option2 value"),
                        option3_name=g(row, "option3 name"),
                        option3_value=g(row, "option3 value"),
                        grams=g(row, "variant grams"),
                        weight_unit=g(row, "variant weight unit"),
                        inventory_qty=g(row, "variant inventory qty"),
                        inventory_policy=g(row, "variant inventory policy"),
                        barcode=g(row, "variant barcode"),
                        requires_shipping=g(row, "variant requires shipping"),
                        taxable=g(row, "variant taxable"),
                        variant_image=g(row, "variant image"),
                    )
                )

    if skipped_rows:
        logger.warning("Skipped %d CSV rows with no handle", skipped_rows)

    # Warn about duplicate SKUs within the same product
    for handle, prod in products.items():
        skus = [v.sku for v in prod.variants if v.sku]
        seen = set()
        for sku in skus:
            if sku in seen:
                logger.warning("Duplicate SKU '%s' in product '%s' (%s)", sku, prod.title, handle)
            seen.add(sku)

    logger.info("Parsed %d products from %s", len(products), path.name)
    return products


# ----------------------------------------------------------------------
# Convert live API products → Product dict
# ----------------------------------------------------------------------

def products_from_api(api_products: list[dict]) -> dict[str, Product]:
    """
    Convert a list of Shopify API product dicts (from GET /products.json)
    into our internal Product format keyed by handle.
    """
    products: dict[str, Product] = {}
    for ap in api_products:
        handle = ap.get("handle", "")
        if not handle:
            continue

        images = []
        image_alts = []
        for img in ap.get("images", []):
            src = img.get("src", "")
            if src:
                images.append(src)
                image_alts.append(img.get("alt") or "")

        variants = []
        for av in ap.get("variants", []):
            options = ap.get("options", [])
            variants.append(
                Variant(
                    sku=av.get("sku") or "",
                    price=str(av.get("price", "")),
                    compare_at_price=str(av.get("compare_at_price") or ""),
                    cost="",  # cost is on inventory_item, not available here
                    option1_name=options[0]["name"] if len(options) > 0 else "",
                    option1_value=av.get("option1") or "",
                    option2_name=options[1]["name"] if len(options) > 1 else "",
                    option2_value=av.get("option2") or "",
                    option3_name=options[2]["name"] if len(options) > 2 else "",
                    option3_value=av.get("option3") or "",
                    grams=str(av.get("grams", "")),
                    weight_unit=av.get("weight_unit") or "",
                    inventory_qty=str(av.get("inventory_quantity", "")),
                    inventory_policy=av.get("inventory_policy") or "",
                    barcode=av.get("barcode") or "",
                    requires_shipping=str(av.get("requires_shipping", "")),
                    taxable=str(av.get("taxable", "")),
                    variant_image="",
                    shopify_variant_id=av.get("id"),
                    shopify_inventory_item_id=av.get("inventory_item_id"),
                )
            )

        tags = ap.get("tags", "")
        products[handle] = Product(
            handle=handle,
            title=ap.get("title", ""),
            body_html=ap.get("body_html") or "",
            vendor=ap.get("vendor", ""),
            product_type=ap.get("product_type", ""),
            tags=tags,
            published="true" if ap.get("status") == "active" else "false",
            status=ap.get("status", ""),
            images=images,
            image_alts=image_alts,
            variants=variants,
            shopify_id=ap.get("id"),
        )

    logger.info("Converted %d API products to internal format", len(products))
    return products


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------

@dataclass
class ProductDiff:
    """What changed on a single product."""
    images_changed: bool = False
    price_changed: bool = False
    compare_at_price_changed: bool = False
    cost_changed: bool = False
    variants_added: bool = False
    variants_removed: bool = False
    added_skus: list[str] = field(default_factory=list)
    removed_skus: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return (self.images_changed or self.price_changed
                or self.compare_at_price_changed or self.cost_changed
                or self.variants_added or self.variants_removed)

    def __str__(self):
        parts = []
        if self.images_changed:
            parts.append("images")
        if self.price_changed:
            parts.append("price")
        if self.compare_at_price_changed:
            parts.append("compare-at-price")
        if self.cost_changed:
            parts.append("cost")
        if self.variants_added:
            parts.append(f"+{len(self.added_skus)} variants")
        if self.variants_removed:
            parts.append(f"-{len(self.removed_skus)} variants")
        return ", ".join(parts) if parts else "none"


@dataclass
class SyncPlan:
    """The result of comparing two catalogues."""
    to_create: list[Product] = field(default_factory=list)
    to_update: list[tuple[Product, Product, ProductDiff]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def to_update_images(self) -> list[tuple[Product, Product, ProductDiff]]:
        return [(old, new, d) for old, new, d in self.to_update if d.images_changed]

    @property
    def to_update_prices(self) -> list[tuple[Product, Product, ProductDiff]]:
        return [(old, new, d) for old, new, d in self.to_update if d.price_changed]

    @property
    def to_update_costs(self) -> list[tuple[Product, Product, ProductDiff]]:
        return [(old, new, d) for old, new, d in self.to_update if d.cost_changed]

    @property
    def to_add_variants(self) -> list[tuple[Product, Product, ProductDiff]]:
        return [(old, new, d) for old, new, d in self.to_update if d.variants_added]

    @property
    def to_remove_variants(self) -> list[tuple[Product, Product, ProductDiff]]:
        return [(old, new, d) for old, new, d in self.to_update if d.variants_removed]

    @property
    def summary(self) -> str:
        lines = [
            f"  New products to create:        {len(self.to_create)}",
            f"  Products needing updates:      {len(self.to_update)}",
            f"    - image updates:             {len(self.to_update_images)}",
            f"    - price updates:             {len(self.to_update_prices)}",
            f"    - cost updates:              {len(self.to_update_costs)}",
            f"    - add variants:              {len(self.to_add_variants)}",
            f"    - remove variants:           {len(self.to_remove_variants)}",
            f"  Unchanged products:            {len(self.unchanged)}",
        ]
        return "\n".join(lines)


def compare_catalogues(
    existing: dict[str, Product],
    new_catalogue: dict[str, Product],
) -> SyncPlan:
    """
    Compare existing Shopify products against the new vendor catalogue.
    Returns a SyncPlan describing what actions are needed.
    """
    plan = SyncPlan()

    for handle, new_prod in new_catalogue.items():
        if handle not in existing:
            plan.to_create.append(new_prod)
            continue

        old_prod = existing[handle]
        diff = _diff_products(old_prod, new_prod)

        if diff.has_changes:
            plan.to_update.append((old_prod, new_prod, diff))
        else:
            plan.unchanged.append(handle)

    return plan


def _diff_products(old: Product, new: Product) -> ProductDiff:
    """Compute what changed between two versions of the same product."""
    diff = ProductDiff()

    # Image comparison
    old_images = _normalise_urls(old.images)
    new_images = _normalise_urls(new.images)
    if old_images != new_images:
        diff.images_changed = True

    # SKU sets
    old_skus = {v.sku for v in old.variants if v.sku}
    new_skus = {v.sku for v in new.variants if v.sku}

    # New / removed variants
    added = new_skus - old_skus
    removed = old_skus - new_skus
    if added:
        diff.variants_added = True
        diff.added_skus = sorted(added)
    if removed:
        diff.variants_removed = True
        diff.removed_skus = sorted(removed)

    # Price comparison (only for SKUs that exist in both)
    old_prices = {v.sku: v.price for v in old.variants if v.sku}
    new_prices = {v.sku: v.price for v in new.variants if v.sku}
    for sku in old_skus & new_skus:
        old_p, new_p = old_prices.get(sku, ""), new_prices.get(sku, "")
        if old_p and new_p and _normalise_price(old_p) != _normalise_price(new_p):
            diff.price_changed = True
            break

    # Compare-at-price comparison
    old_cap = {v.sku: v.compare_at_price for v in old.variants if v.sku}
    new_cap = {v.sku: v.compare_at_price for v in new.variants if v.sku}
    for sku in old_skus & new_skus:
        old_c, new_c = old_cap.get(sku, ""), new_cap.get(sku, "")
        if _normalise_price(old_c or "0") != _normalise_price(new_c or "0"):
            diff.compare_at_price_changed = True
            break

    # Cost comparison
    old_costs = {v.sku: v.cost for v in old.variants if v.sku}
    new_costs = {v.sku: v.cost for v in new.variants if v.sku}
    for sku, new_cost in new_costs.items():
        if not new_cost:
            continue
        old_cost = old_costs.get(sku, "")
        if not old_cost or _normalise_price(old_cost) != _normalise_price(new_cost):
            diff.cost_changed = True
            break

    return diff


def _normalise_urls(urls: list[str]) -> list[str]:
    """Strip query strings for comparison (Shopify CDN adds cache-busters)."""
    return [u.split("?")[0].strip().rstrip("/") for u in urls]


def _normalise_price(price: str) -> str:
    """Normalise price strings like '10.00', '10', '10.0' → '10.00'."""
    try:
        return f"{float(price):.2f}"
    except (ValueError, TypeError):
        return price.strip()


# ----------------------------------------------------------------------
# Build Shopify API payload from a Product
# ----------------------------------------------------------------------

def _build_variant_dict(v: Variant) -> dict:
    """Build a Shopify variant dict from our internal Variant."""
    vd: dict = {}
    if v.sku:
        vd["sku"] = v.sku
    if v.price:
        vd["price"] = v.price
    if v.compare_at_price:
        vd["compare_at_price"] = v.compare_at_price
    # Note: cost is NOT set here — Shopify ignores it on the variant object.
    # Cost must be set via PUT /inventory_items/{id}.json after creation.
    if v.option1_value:
        vd["option1"] = v.option1_value
    if v.option2_value:
        vd["option2"] = v.option2_value
    if v.option3_value:
        vd["option3"] = v.option3_value
    if v.grams:
        vd["grams"] = int(float(v.grams))
    if v.weight_unit:
        vd["weight_unit"] = v.weight_unit
    if v.barcode:
        vd["barcode"] = v.barcode
    if v.requires_shipping:
        vd["requires_shipping"] = v.requires_shipping.lower() == "true"
    if v.taxable:
        vd["taxable"] = v.taxable.lower() == "true"
    if v.inventory_policy:
        vd["inventory_policy"] = v.inventory_policy
    # Enable inventory tracking so Shopify counts stock
    vd["inventory_management"] = "shopify"
    return vd


def product_to_shopify_payload(product: Product) -> dict:
    """
    Convert a parsed catalogue Product into a dict suitable for
    POST /admin/api/.../products.json
    """
    payload: dict = {
        "title": product.title,
        "handle": product.handle,
        "body_html": product.body_html,
        "vendor": product.vendor,
        "product_type": product.product_type,
        "tags": product.tags,
        "status": product.status.lower() if product.status else "active",
    }

    if product.variants:
        payload["variants"] = [_build_variant_dict(v) for v in product.variants]

        # Derive option names from the first variant that has them
        options = []
        for v in product.variants:
            names = [v.option1_name, v.option2_name, v.option3_name]
            if any(names):
                for i, name in enumerate(names, 1):
                    if name:
                        options.append({"name": name, "position": i})
                break
        if options:
            payload["options"] = options

    if product.images:
        payload["images"] = [
            {"src": url, "alt": alt}
            for url, alt in zip(product.images, product.image_alts)
        ]

    return payload


def variant_to_shopify_payload(v: Variant) -> dict:
    """
    Build a payload suitable for POST /products/{id}/variants.json
    (adding a new variant to an existing product).
    """
    return _build_variant_dict(v)
