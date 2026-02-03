"""
Shopify Admin REST API client.

Handles authentication, rate limiting, pagination, and all product/image
operations needed for the WYN catalogue sync.
"""

import time
import logging
from urllib.parse import urljoin, urlparse, parse_qs

import requests

logger = logging.getLogger(__name__)

API_VERSION = "2024-01"


class ShopifyAPIError(Exception):
    """Raised when a Shopify API call fails."""

    def __init__(self, status_code, body, message=""):
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"Shopify API {status_code}: {body}")


class ShopifyClient:
    """
    Thin wrapper around the Shopify Admin REST API.

    Handles:
    - Authentication via access token
    - Automatic retry with back-off on 429 (rate limit) responses
    - Cursor-based pagination for listing endpoints
    """

    def __init__(self, store_url: str, access_token: str):
        # Normalise store URL to just the hostname
        store_url = store_url.strip().rstrip("/")
        if store_url.startswith("http"):
            store_url = urlparse(store_url).hostname
        self.base = f"https://{store_url}/admin/api/{API_VERSION}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Low-level request with rate-limit handling
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base}/{path.lstrip('/')}"
        for attempt in range(5):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2))
                logger.warning("Rate limited – sleeping %.1fs (attempt %d)", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue
            if resp.status_code >= 400:
                raise ShopifyAPIError(resp.status_code, resp.text)
            return resp
        raise ShopifyAPIError(429, "Rate limit not resolved after retries")

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, json_body):
        return self._request("POST", path, json=json_body)

    def _put(self, path, json_body):
        return self._request("PUT", path, json=json_body)

    def _delete(self, path):
        return self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    def _paginate(self, path, key, params=None):
        """Yield all items across paginated responses using Link-header cursors."""
        params = dict(params or {})
        params.setdefault("limit", 250)
        while True:
            resp = self._get(path, params=params)
            data = resp.json()
            items = data.get(key, [])
            yield from items
            # Follow cursor pagination via Link header
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            # Extract the next page URL
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split("<")[1].split(">")[0]
                    # Extract page_info param
                    qs = parse_qs(urlparse(next_url).query)
                    params = {"limit": params["limit"], "page_info": qs["page_info"][0]}
                    break

    # ------------------------------------------------------------------
    # Product operations
    # ------------------------------------------------------------------

    def get_all_products(self, vendor: str = None) -> list[dict]:
        """Fetch every product, optionally filtered by vendor."""
        params = {}
        if vendor:
            params["vendor"] = vendor
        products = list(self._paginate("products.json", "products", params))
        logger.info("Fetched %d products from Shopify (vendor=%s)", len(products), vendor or "ALL")
        return products

    def get_product(self, product_id: int) -> dict:
        return self._get(f"products/{product_id}.json").json()["product"]

    def create_product(self, product_payload: dict) -> dict:
        """Create a product. Returns the created product dict."""
        resp = self._post("products.json", {"product": product_payload})
        product = resp.json()["product"]
        logger.info("Created product %s (id=%s)", product.get("title"), product["id"])
        return product

    def update_product(self, product_id: int, updates: dict) -> dict:
        resp = self._put(f"products/{product_id}.json", {"product": updates})
        product = resp.json()["product"]
        logger.info("Updated product id=%s", product_id)
        return product

    # ------------------------------------------------------------------
    # Image operations
    # ------------------------------------------------------------------

    def get_product_images(self, product_id: int) -> list[dict]:
        return list(self._paginate(f"products/{product_id}/images.json", "images"))

    def add_product_image(self, product_id: int, image_url: str, position: int = None, alt: str = "") -> dict:
        payload = {"image": {"src": image_url}}
        if position is not None:
            payload["image"]["position"] = position
        if alt:
            payload["image"]["alt"] = alt
        resp = self._post(f"products/{product_id}/images.json", payload)
        img = resp.json()["image"]
        logger.info("Added image to product %d (image_id=%s)", product_id, img["id"])
        return img

    def delete_product_image(self, product_id: int, image_id: int):
        self._delete(f"products/{product_id}/images/{image_id}.json")
        logger.info("Deleted image %d from product %d", image_id, product_id)

    def replace_product_images(self, product_id: int, new_image_urls: list[str]):
        """
        Delete all existing images on a product, then upload the new ones
        in order.  This is the safest way to guarantee images match the
        catalogue exactly.
        """
        existing = self.get_product_images(product_id)
        for img in existing:
            self.delete_product_image(product_id, img["id"])
        for pos, url in enumerate(new_image_urls, start=1):
            self.add_product_image(product_id, url, position=pos)

    # ------------------------------------------------------------------
    # Variant operations
    # ------------------------------------------------------------------

    def update_variant(self, variant_id: int, updates: dict) -> dict:
        resp = self._put(f"variants/{variant_id}.json", {"variant": updates})
        return resp.json()["variant"]

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Return shop info to verify credentials work."""
        resp = self._get("shop.json")
        shop = resp.json()["shop"]
        logger.info("Connected to Shopify store: %s (%s)", shop["name"], shop["myshopify_domain"])
        return shop
