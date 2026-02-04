"""
Supabase integration for sync audit logging and state tracking.

Stores a record of every sync run (what was created, updated, failed)
so you have a full history of changes to the Shopify store.  Also
caches product state snapshots to enable faster diffs on subsequent runs.

Tables (auto-created on first run via the setup() function):
    sync_runs        — one row per sync invocation
    sync_actions     — one row per product created/updated/failed
    product_snapshots — latest known state of each product (handle, images, prices)
"""

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Optional dependency — gracefully degrade if not installed
try:
    from supabase import create_client, Client
    HAS_SUPABASE = True
except ImportError:
    HAS_SUPABASE = False
    Client = None


class SupabaseLogger:
    """
    Logs sync operations to Supabase for audit trail and state tracking.
    If Supabase credentials are not configured, all methods silently no-op.
    """

    def __init__(self):
        self.client: Client | None = None
        self.run_id: str | None = None

        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")

        if not url or not key:
            logger.info("Supabase not configured — audit logging disabled.")
            return

        if not HAS_SUPABASE:
            logger.warning("supabase-py not installed — run: pip install supabase")
            return

        try:
            self.client = create_client(url, key)
            logger.info("Connected to Supabase for audit logging.")
        except Exception as e:
            logger.warning("Failed to connect to Supabase: %s", e)
            self.client = None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # ------------------------------------------------------------------
    # Sync run lifecycle
    # ------------------------------------------------------------------

    def start_run(self, action: str, vendor: str, catalogue_path: str) -> str | None:
        """Record the start of a sync run. Returns run_id."""
        if not self.enabled:
            return None
        try:
            row = {
                "action": action,
                "vendor": vendor,
                "catalogue_path": catalogue_path,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            result = self.client.table("sync_runs").insert(row).execute()
            self.run_id = result.data[0]["id"]
            logger.info("Supabase sync run started: %s", self.run_id)
            return self.run_id
        except Exception as e:
            logger.warning("Supabase start_run failed: %s", e)
            return None

    def finish_run(self, status: str, report: dict):
        """Mark the current run as complete with a summary."""
        if not self.enabled or not self.run_id:
            return
        try:
            self.client.table("sync_runs").update({
                "status": status,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "report_summary": json.dumps({
                    "created": len(report.get("created", [])),
                    "images_updated": len(report.get("images_updated", [])),
                    "prices_updated": len(report.get("prices_updated", [])),
                    "costs_updated": len(report.get("costs_updated", [])),
                    "variants_added": len(report.get("variants_added", [])),
                    "errors": len(report.get("errors", [])),
                }),
            }).eq("id", self.run_id).execute()
        except Exception as e:
            logger.warning("Supabase finish_run failed: %s", e)

    # ------------------------------------------------------------------
    # Per-product action logging
    # ------------------------------------------------------------------

    def log_action(self, action: str, handle: str, title: str,
                   shopify_id: int = None, details: dict = None):
        """Log a single product-level action (create, update_images, etc.)."""
        if not self.enabled:
            return
        try:
            row = {
                "run_id": self.run_id,
                "action": action,
                "handle": handle,
                "title": title,
                "shopify_id": shopify_id,
                "details": json.dumps(details or {}),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.client.table("sync_actions").insert(row).execute()
        except Exception as e:
            logger.debug("Supabase log_action failed: %s", e)

    def log_error(self, handle: str, title: str, error: str):
        """Log a failed operation."""
        self.log_action("error", handle, title, details={"error": error})

    # ------------------------------------------------------------------
    # Product state snapshots
    # ------------------------------------------------------------------

    def save_snapshot(self, handle: str, title: str, shopify_id: int,
                      image_urls: list[str], variant_skus: list[str],
                      prices: dict[str, str]):
        """Upsert a product state snapshot (for faster future diffs)."""
        if not self.enabled:
            return
        try:
            row = {
                "handle": handle,
                "title": title,
                "shopify_id": shopify_id,
                "image_urls": json.dumps(image_urls),
                "variant_skus": json.dumps(variant_skus),
                "prices": json.dumps(prices),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.client.table("product_snapshots").upsert(
                row, on_conflict="handle"
            ).execute()
        except Exception as e:
            logger.debug("Supabase save_snapshot failed: %s", e)

    def get_snapshots(self) -> dict[str, dict]:
        """Fetch all product snapshots. Returns {handle: snapshot_dict}."""
        if not self.enabled:
            return {}
        try:
            result = self.client.table("product_snapshots").select("*").execute()
            return {row["handle"]: row for row in result.data}
        except Exception as e:
            logger.warning("Supabase get_snapshots failed: %s", e)
            return {}

    # ------------------------------------------------------------------
    # History queries
    # ------------------------------------------------------------------

    def get_recent_runs(self, limit: int = 10) -> list[dict]:
        """Fetch recent sync runs for display."""
        if not self.enabled:
            return []
        try:
            result = (
                self.client.table("sync_runs")
                .select("*")
                .order("started_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data
        except Exception as e:
            logger.warning("Supabase get_recent_runs failed: %s", e)
            return []

    def get_run_actions(self, run_id: str) -> list[dict]:
        """Fetch all actions for a specific run."""
        if not self.enabled:
            return []
        try:
            result = (
                self.client.table("sync_actions")
                .select("*")
                .eq("run_id", run_id)
                .order("timestamp")
                .execute()
            )
            return result.data
        except Exception as e:
            logger.warning("Supabase get_run_actions failed: %s", e)
            return []


# ------------------------------------------------------------------
# SQL for table creation (run once in Supabase SQL editor)
# ------------------------------------------------------------------

SETUP_SQL = """
-- Sync run history
CREATE TABLE IF NOT EXISTS sync_runs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    action TEXT NOT NULL,
    vendor TEXT NOT NULL,
    catalogue_path TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    report_summary JSONB
);

-- Per-product action log
CREATE TABLE IF NOT EXISTS sync_actions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    run_id UUID REFERENCES sync_runs(id),
    action TEXT NOT NULL,
    handle TEXT NOT NULL,
    title TEXT,
    shopify_id BIGINT,
    details JSONB,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Product state cache (for faster diffs)
CREATE TABLE IF NOT EXISTS product_snapshots (
    handle TEXT PRIMARY KEY,
    title TEXT,
    shopify_id BIGINT,
    image_urls JSONB,
    variant_skus JSONB,
    prices JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_sync_actions_run_id ON sync_actions(run_id);
CREATE INDEX IF NOT EXISTS idx_sync_actions_handle ON sync_actions(handle);
CREATE INDEX IF NOT EXISTS idx_sync_runs_status ON sync_runs(status);
"""


def print_setup_sql():
    """Print the SQL needed to set up the Supabase tables."""
    print("Run this SQL in your Supabase SQL Editor:")
    print("=" * 60)
    print(SETUP_SQL)
    print("=" * 60)
