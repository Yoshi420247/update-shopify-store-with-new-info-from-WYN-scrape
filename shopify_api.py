"""
Shopify Admin API client (REST + GraphQL).

Handles authentication, rate limiting, pagination, and all product/image/
publishing operations needed for the WYN catalogue sync.
"""

import json
import time
import logging
from urllib.parse import urlparse, parse_qs

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
    Wrapper around the Shopify Admin REST + GraphQL APIs.

    Handles:
    - Authentication via access token
    - Automatic retry with back-off on 429 (rate limit) responses
    - Cursor-based pagination for listing endpoints
    - GraphQL mutations for publishing and inventory cost updates
    """

    def __init__(self, store_url: str, access_token: str):
        store_url = store_url.strip().rstrip("/")
        if store_url.startswith("http"):
            store_url = urlparse(store_url).hostname
        self.hostname = store_url
        self.base = f"https://{store_url}/admin/api/{API_VERSION}"
        self.graphql_url = f"https://{store_url}/admin/api/{API_VERSION}/graphql.json"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            }
        )
        # Minimum delay between requests to stay under rate limits
        self._last_request_time = 0.0
        self._min_interval = 0.55  # seconds

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base}/{path.lstrip('/')}"
        for attempt in range(5):
            self._throttle()
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
    # GraphQL
    # ------------------------------------------------------------------

    def _graphql(self, query: str, variables: dict = None) -> dict:
        """Execute a GraphQL query/mutation."""
        body = {"query": query}
        if variables:
            body["variables"] = variables
        for attempt in range(5):
            self._throttle()
            resp = self.session.post(self.graphql_url, json=body)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2))
                logger.warning("GraphQL rate limited – sleeping %.1fs (attempt %d)", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue
            if resp.status_code >= 400:
                raise ShopifyAPIError(resp.status_code, resp.text)
            data = resp.json()
            if "errors" in data:
                raise ShopifyAPIError(200, json.dumps(data["errors"]), "GraphQL errors")
            return data["data"]
        raise ShopifyAPIError(429, "GraphQL rate limit not resolved after retries")

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
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split("<")[1].split(">")[0]
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
        """Delete all existing images then upload new ones in order."""
        existing = self.get_product_images(product_id)
        for img in existing:
            self.delete_product_image(product_id, img["id"])
        for pos, url in enumerate(new_image_urls, start=1):
            self.add_product_image(product_id, url, position=pos)

    # ------------------------------------------------------------------
    # Variant operations
    # ------------------------------------------------------------------

    def create_variant(self, product_id: int, variant_payload: dict) -> dict:
        """Add a new variant to an existing product."""
        resp = self._post(f"products/{product_id}/variants.json", {"variant": variant_payload})
        variant = resp.json()["variant"]
        logger.info("Created variant (id=%s, sku=%s) on product %d",
                     variant["id"], variant.get("sku"), product_id)
        return variant

    def update_variant(self, variant_id: int, updates: dict) -> dict:
        resp = self._put(f"variants/{variant_id}.json", {"variant": updates})
        return resp.json()["variant"]

    def delete_variant(self, product_id: int, variant_id: int):
        self._delete(f"products/{product_id}/variants/{variant_id}.json")
        logger.info("Deleted variant %d from product %d", variant_id, product_id)

    # ------------------------------------------------------------------
    # Inventory operations
    # ------------------------------------------------------------------

    def get_inventory_item(self, inventory_item_id: int) -> dict:
        resp = self._get(f"inventory_items/{inventory_item_id}.json")
        return resp.json()["inventory_item"]

    def update_inventory_item_cost(self, inventory_item_id: int, cost: str) -> dict:
        resp = self._put(
            f"inventory_items/{inventory_item_id}.json",
            {"inventory_item": {"cost": cost}},
        )
        return resp.json()["inventory_item"]

    def get_inventory_levels(self, inventory_item_id: int) -> list[dict]:
        resp = self._get("inventory_levels.json", params={"inventory_item_ids": inventory_item_id})
        return resp.json().get("inventory_levels", [])

    def set_inventory_level(self, inventory_item_id: int, location_id: int, available: int) -> dict:
        resp = self._post("inventory_levels/set.json", {
            "inventory_item_id": inventory_item_id,
            "location_id": location_id,
            "available": available,
        })
        return resp.json().get("inventory_level", {})

    def get_locations(self) -> list[dict]:
        """Fetch all locations for the store."""
        return list(self._paginate("locations.json", "locations"))

    def set_costs_on_product(self, product: dict, cost_map: dict[str, str]):
        """
        Set cost on inventory items for a product's variants.
        cost_map: {sku: cost_string}
        """
        for variant in product.get("variants", []):
            sku = variant.get("sku", "")
            cost = cost_map.get(sku)
            if cost and variant.get("inventory_item_id"):
                self.update_inventory_item_cost(variant["inventory_item_id"], cost)
                logger.info("Set cost %s on SKU %s (inv_item=%d)",
                           cost, sku, variant["inventory_item_id"])

    def enable_inventory_tracking(self, product: dict):
        """
        Enable Shopify inventory tracking on all variants of a product
        and set their inventory_management to 'shopify'.
        """
        for variant in product.get("variants", []):
            if variant.get("inventory_management") != "shopify":
                self.update_variant(variant["id"], {"inventory_management": "shopify"})

    # ------------------------------------------------------------------
    # Publishing (GraphQL)
    # ------------------------------------------------------------------

    def get_publication_ids(self) -> list[dict]:
        """Fetch all publications (sales channels) for the store."""
        query = """
        {
            publications(first: 20) {
                edges {
                    node {
                        id
                        name
                    }
                }
            }
        }
        """
        data = self._graphql(query)
        pubs = []
        for edge in data["publications"]["edges"]:
            pubs.append({"id": edge["node"]["id"], "name": edge["node"]["name"]})
        return pubs

    def publish_product(self, product_id: int, publication_id: str) -> bool:
        """Publish a product to a sales channel using GraphQL."""
        gid = f"gid://shopify/Product/{product_id}"
        query = """
        mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
            publishablePublish(id: $id, input: $input) {
                publishable {
                    availablePublicationsCount {
                        count
                    }
                }
                userErrors {
                    field
                    message
                }
            }
        }
        """
        variables = {
            "id": gid,
            "input": [{"publicationId": publication_id}],
        }
        data = self._graphql(query, variables)
        errors = data["publishablePublish"]["userErrors"]
        if errors:
            logger.error("Publish errors for product %d: %s", product_id, errors)
            return False
        logger.info("Published product %d to %s", product_id, publication_id)
        return True

    def publish_products_to_online_store(self, product_ids: list[int]) -> int:
        """
        Publish a list of products to the Online Store channel.
        Returns count of successfully published products.
        """
        pubs = self.get_publication_ids()
        online_store = next((p for p in pubs if "online store" in p["name"].lower()), None)
        if not online_store:
            logger.error("Could not find Online Store publication. Available: %s",
                         [p["name"] for p in pubs])
            return 0

        pub_id = online_store["id"]
        logger.info("Publishing %d products to '%s' (%s)", len(product_ids), online_store["name"], pub_id)

        success = 0
        for pid in product_ids:
            try:
                if self.publish_product(pid, pub_id):
                    success += 1
            except ShopifyAPIError as e:
                logger.error("Failed to publish product %d: %s", pid, e)
        return success

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Return shop info to verify credentials work."""
        resp = self._get("shop.json")
        shop = resp.json()["shop"]
        logger.info("Connected to Shopify store: %s (%s)", shop["name"], shop["myshopify_domain"])
        return shop
