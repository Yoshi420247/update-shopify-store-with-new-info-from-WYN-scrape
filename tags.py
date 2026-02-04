"""
Auto-tagging engine for WYN (What You Need) products.

Applies the Oil Slick tag taxonomy (family:, pillar:, use:, material:,
brand:, style:, joint_size:, joint_gender:) based on keyword matching
in product titles.  Tags drive smart-collection auto-sorting — once a
product is correctly tagged it shows up in the right collections
automatically.
"""

import re
import logging

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _discard_prefix(tag_set: set, prefix: str):
    """Remove any tags from the set that start with prefix."""
    to_remove = [t for t in tag_set if t.startswith(prefix)]
    for t in to_remove:
        tag_set.discard(t)


class _TagSet(set):
    """A set with a convenience method for removing namespaced tags."""
    def discard_prefix(self, prefix: str):
        _discard_prefix(self, prefix)


# ------------------------------------------------------------------
# Category rules  (pattern -> family, pillar, use)
# Checked top-to-bottom; first match wins for family/pillar/use.
# ------------------------------------------------------------------

CATEGORY_RULES: list[dict] = [
    # Dabbing devices
    {"pattern": re.compile(r"nectar collector", re.I), "family": "nectar-collector", "pillar": "smokeshop-device", "use": "dabbing"},
    {"pattern": re.compile(r"e[- ]?rig|electronic rig", re.I), "family": "e-rig", "pillar": "smokeshop-device", "use": "dabbing"},
    {"pattern": re.compile(r"silicone.*(rig|recycler)", re.I), "family": "silicone-rig", "pillar": "smokeshop-device", "use": "dabbing"},
    {"pattern": re.compile(r"\b(dab rig|oil rig|recycler|rig)\b", re.I), "family": "glass-rig", "pillar": "smokeshop-device", "use": "dabbing"},

    # Flower smoking devices
    {"pattern": re.compile(r"bubbler", re.I), "family": "bubbler", "pillar": "smokeshop-device", "use": "flower-smoking"},
    {"pattern": re.compile(r"steamroller", re.I), "family": "steamroller", "pillar": "smokeshop-device", "use": "flower-smoking"},
    {"pattern": re.compile(r"chillum|one.?hitter", re.I), "family": "chillum-onehitter", "pillar": "smokeshop-device", "use": "flower-smoking"},
    {"pattern": re.compile(r"water pipe|bong", re.I), "family": "glass-bong", "pillar": "smokeshop-device", "use": "flower-smoking"},
    {"pattern": re.compile(r"hand pipe|glass pipe|spoon|sherlock|straight tube", re.I), "family": "spoon-pipe", "pillar": "smokeshop-device", "use": "flower-smoking"},

    # Dabbing accessories
    {"pattern": re.compile(r"banger|quartz nail", re.I), "family": "banger", "pillar": "accessory", "use": "dabbing"},
    {"pattern": re.compile(r"carb cap", re.I), "family": "carb-cap", "pillar": "accessory", "use": "dabbing"},
    {"pattern": re.compile(r"dab tool|dabber|wax tool", re.I), "family": "dab-tool", "pillar": "accessory", "use": "dabbing"},
    {"pattern": re.compile(r"torch", re.I), "family": "torch", "pillar": "accessory", "use": "dabbing"},

    # Flower accessories
    {"pattern": re.compile(r"ash.?catcher", re.I), "family": "ash-catcher", "pillar": "accessory", "use": "flower-smoking"},
    {"pattern": re.compile(r"downstem", re.I), "family": "downstem", "pillar": "accessory", "use": "flower-smoking"},
    {"pattern": re.compile(r"\b(bowl|flower bowl|glass bowl)\b", re.I), "family": "flower-bowl", "pillar": "accessory", "use": "flower-smoking"},
    {"pattern": re.compile(r"ashtray", re.I), "family": "storage-accessory", "pillar": "accessory", "use": "storage"},
    {"pattern": re.compile(r"roach clip", re.I), "family": "dab-tool", "pillar": "accessory", "use": "flower-smoking"},

    # Rolling
    {"pattern": re.compile(r"rolling paper|cone|pre.?roll", re.I), "family": "rolling-paper", "pillar": "accessory", "use": "rolling"},
    {"pattern": re.compile(r"rolling tray|tray", re.I), "family": "rolling-tray", "pillar": "accessory", "use": "rolling"},
    {"pattern": re.compile(r"rolling machine|roller", re.I), "family": "rolling-machine", "pillar": "accessory", "use": "rolling"},

    # Vaping
    {"pattern": re.compile(r"battery|vape|cbd battery", re.I), "family": "vape-battery", "pillar": "smokeshop-device", "use": "vaping"},
    {"pattern": re.compile(r"cartridge", re.I), "family": "vape-cartridge", "pillar": "accessory", "use": "vaping"},

    # Preparation
    {"pattern": re.compile(r"grinder", re.I), "family": "grinder", "pillar": "accessory", "use": "preparation"},
    {"pattern": re.compile(r"scale", re.I), "family": "scale", "pillar": "accessory", "use": "preparation"},

    # Storage
    {"pattern": re.compile(r"jar|container|stash", re.I), "family": "container", "pillar": "accessory", "use": "storage"},

    # Merch
    {"pattern": re.compile(r"pendant|necklace", re.I), "family": "merch-pendant", "pillar": "merch", "use": ""},
    {"pattern": re.compile(r"apparel|shirt|hat|hoodie", re.I), "family": "merch-apparel", "pillar": "merch", "use": ""},
]

# ------------------------------------------------------------------
# Material detection
# ------------------------------------------------------------------

MATERIAL_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"silicone", re.I), "silicone"),
    (re.compile(r"borosilicate", re.I), "borosilicate"),
    (re.compile(r"quartz", re.I), "quartz"),
    (re.compile(r"titanium", re.I), "titanium"),
    (re.compile(r"ceramic", re.I), "ceramic"),
    (re.compile(r"wood|wooden", re.I), "wood"),
    (re.compile(r"steel|metal|zinc|aluminum", re.I), "metal"),
    (re.compile(r"glass", re.I), "glass"),  # last — many things contain "glass"
]

# ------------------------------------------------------------------
# Brand detection
# ------------------------------------------------------------------

BRAND_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bzig.?zag\b", re.I), "zig-zag"),
    (re.compile(r"\bvibes\b", re.I), "vibes"),
    (re.compile(r"\bmonark\b", re.I), "monark"),
    (re.compile(r"\bcookies\b", re.I), "cookies"),
    (re.compile(r"\bmaven\b", re.I), "maven"),
    (re.compile(r"\braw\b", re.I), "raw"),
    (re.compile(r"\belements\b", re.I), "elements"),
    (re.compile(r"\bpuffco\b", re.I), "puffco"),
    (re.compile(r"\blookah\b", re.I), "lookah"),
    (re.compile(r"\bg.?pen\b", re.I), "g-pen"),
    (re.compile(r"\b710.?sci\b", re.I), "710-sci"),
    (re.compile(r"\bscorch\b", re.I), "scorch"),
    (re.compile(r"\bonly.?quartz\b", re.I), "only-quartz"),
    (re.compile(r"\beo.?vape\b", re.I), "eo-vape"),
]

# ------------------------------------------------------------------
# Style / theme detection (multiple can match)
# ------------------------------------------------------------------

STYLE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(cat|dog|husky|pug|bulldog|lion|gorilla|beaver|penguin|duck|dolphin|shark|octopus|frog|turtle|owl|fox|bear|bunny)\b", re.I), "animal"),
    (re.compile(r"\b(mario|sonic|yoda|grogu|rick|kenny|homer|marge|kuromi|kitty|minnie|scooby|spider.?man|labubu|pikachu|spongebob|goku)\b", re.I), "character"),
    (re.compile(r"\b(skull|zombie|witch|ghost|mummy|headless|corpse|skeleton|pumpkin)\b", re.I), "halloween"),
    (re.compile(r"\b(soccer|baseball|football|basketball|sports|messi)\b", re.I), "sports"),
    (re.compile(r"\belectric\b", re.I), "electric"),
    (re.compile(r"\bmade in usa|american made\b", re.I), "made-in-usa"),
    (re.compile(r"\bheady\b", re.I), "heady"),
    (re.compile(r"\bmini\b|\bportable\b|\btravel\b", re.I), "travel-friendly"),
]

# ------------------------------------------------------------------
# Joint size / gender detection
# ------------------------------------------------------------------

JOINT_SIZE_RE = re.compile(r"\b(10|14|18)\s*mm\b", re.I)
JOINT_GENDER_MALE = re.compile(r"\bmale\b", re.I)
JOINT_GENDER_FEMALE = re.compile(r"\bfemale\b", re.I)


# ------------------------------------------------------------------
# Pricing
# ------------------------------------------------------------------

MARKUP_MULTIPLIER = 2.0  # Retail = cost * 2


def calculate_retail_price(cost: str) -> str | None:
    """Return retail price (2x cost) as a string, or None if cost is invalid."""
    try:
        return f"{float(cost) * MARKUP_MULTIPLIER:.2f}"
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def generate_tags(title: str, existing_tags: str = "", product_type: str = "") -> str:
    """
    Given a product title (and optionally existing tags and product_type),
    return a complete comma-separated tag string with the Oil Slick taxonomy
    applied.

    Existing tags that don't conflict with the generated ones are preserved.
    """
    text = f"{title} {product_type}".strip()
    tags = _TagSet()

    if existing_tags:
        for t in existing_tags.split(","):
            t = t.strip()
            if t:
                tags.add(t)

    # Category (first match wins)
    matched_category = False
    for rule in CATEGORY_RULES:
        if rule["pattern"].search(text):
            tags.discard_prefix("family:")
            tags.discard_prefix("pillar:")
            tags.discard_prefix("use:")
            tags.add(f"family:{rule['family']}")
            tags.add(f"pillar:{rule['pillar']}")
            if rule.get("use"):
                tags.add(f"use:{rule['use']}")
            matched_category = True
            break

    if not matched_category:
        logger.debug("No category match for: %s", title)

    # Materials (multiple allowed)
    for pattern, material in MATERIAL_RULES:
        if pattern.search(text):
            tags.add(f"material:{material}")

    # Brand (first match wins)
    for pattern, brand in BRAND_RULES:
        if pattern.search(text):
            tags.discard_prefix("brand:")
            tags.add(f"brand:{brand}")
            tags.add("style:brand-highlight")
            break

    # Styles (multiple allowed)
    for pattern, style in STYLE_RULES:
        if pattern.search(text):
            tags.add(f"style:{style}")

    # Joint specs
    size_match = JOINT_SIZE_RE.search(text)
    if size_match:
        tags.add(f"joint_size:{size_match.group(1)}mm")

    if JOINT_GENDER_MALE.search(text) and not JOINT_GENDER_FEMALE.search(text):
        tags.add("joint_gender:male")
    elif JOINT_GENDER_FEMALE.search(text):
        tags.add("joint_gender:female")

    return ", ".join(sorted(tags))
