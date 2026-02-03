"""
Shopify CSV catalogue parser and comparison engine.

Parses standard Shopify product-export CSVs (where one product can span
multiple rows for variants and images) and compares two catalogues to
determine what needs to be created, updated, or left alone.
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
    # Preserve all raw rows for any columns we don't explicitly model
    _raw_rows: list[dict] = field(default_factory=list, repr=False)


# ----------------------------------------------------------------------
# CSV parsing
# ----------------------------------------------------------------------

def _normalise_key(col: str) -> str:
    """Lowercase, strip whitespace, collapse spaces."""
    return " ".join(col.strip().lower().split())


def parse_shopify_csv(path: str | Path, vendor_filter: str = None) -> dict[str, Product]:
    """
    Parse a Shopify product-export CSV into a dict keyed by handle.

    In the Shopify CSV format:
    - The first row for a product carries the Title, Handle, Body, etc.
    - Subsequent rows for the same product have blank Title/Handle and
      carry additional variants and/or images.

    Returns {handle: Product}.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    products: dict[str, Product] = {}
    current_handle: str | None = None

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Build a normalised-key -> original-key map so we tolerate
        # slight header variations between exports.
        field_map = {_normalise_key(c): c for c in reader.fieldnames} if reader.fieldnames else {}

        def g(row: dict, normalised_name: str) -> str:
            """Get a value from row using a normalised column name."""
            original = field_map.get(normalised_name, "")
            return (row.get(original) or "").strip()

        for row in reader:
            handle = g(row, "handle")

            # Continuation row (variant / extra image for current product)
            if not handle:
                handle = current_handle
            if not handle:
                continue

            current_handle = handle

            # Apply vendor filter early to skip irrelevant products
            row_vendor = g(row, "vendor")
            if vendor_filter and handle not in products and row_vendor:
                if row_vendor.lower() != vendor_filter.lower():
                    continue

            # First time seeing this handle → create product
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

            # Collect images (skip blanks and duplicates)
            img = g(row, "image src")
            if img and img not in prod.images:
                prod.images.append(img)
                prod.image_alts.append(g(row, "image alt text"))

            # Collect variant
            sku = g(row, "variant sku")
            price = g(row, "variant price")
            if sku or price:
                prod.variants.append(
                    Variant(
                        sku=sku,
                        price=price,
                        compare_at_price=g(row, "variant compare at price"),
                        cost=g(row, "cost per item"),
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

    logger.info("Parsed %d products from %s", len(products), path.name)
    return products


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------

@dataclass
class SyncPlan:
    """The result of comparing two catalogues."""
    to_create: list[Product] = field(default_factory=list)     # In new catalogue only
    to_update_images: list[tuple[Product, Product]] = field(default_factory=list)  # (existing, new) with different images
    unchanged: list[str] = field(default_factory=list)         # Handles that are identical

    @property
    def summary(self) -> str:
        lines = [
            f"  New products to create:     {len(self.to_create)}",
            f"  Products needing image update: {len(self.to_update_images)}",
            f"  Unchanged products:          {len(self.unchanged)}",
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

        # Compare image sets (normalise URLs for comparison)
        old_images = _normalise_urls(old_prod.images)
        new_images = _normalise_urls(new_prod.images)

        if old_images != new_images:
            plan.to_update_images.append((old_prod, new_prod))
        else:
            plan.unchanged.append(handle)

    return plan


def _normalise_urls(urls: list[str]) -> list[str]:
    """
    Strip query strings and normalise for comparison.
    Shopify CDN URLs often have ?v=XXXXX cache-busters that differ
    between exports even when the image hasn't changed.
    """
    result = []
    for u in urls:
        base = u.split("?")[0].strip().rstrip("/")
        # Also strip the cdn.shopify.com size suffix like _1024x1024
        result.append(base)
    return result


# ----------------------------------------------------------------------
# Build Shopify API payload from a Product
# ----------------------------------------------------------------------

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

    # Variants
    if product.variants:
        variant_list = []
        for v in product.variants:
            vd: dict = {}
            if v.sku:
                vd["sku"] = v.sku
            if v.price:
                vd["price"] = v.price
            if v.compare_at_price:
                vd["compare_at_price"] = v.compare_at_price
            if v.cost:
                vd["cost"] = v.cost
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
            variant_list.append(vd)
        payload["variants"] = variant_list

        # Options (derive from the first variant that has option names)
        options = []
        sample = product.variants[0]
        for i, name in enumerate([sample.option1_name, sample.option2_name, sample.option3_name], 1):
            if name:
                options.append({"name": name, "position": i})
        if options:
            payload["options"] = options

    # Images
    if product.images:
        payload["images"] = [
            {"src": url, "alt": alt}
            for url, alt in zip(product.images, product.image_alts)
        ]

    return payload
