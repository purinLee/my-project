"""Reproducible synthetic data generator.

Everything here is SYNTHETIC: fictional merchants, generated products and generated requests.
Run `python scripts/seed_data.py --help` for the CLI. The same (seed, as_of) always yields the
same dataset, which the unit tests rely on.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .classifier import recommend_priority
from .config import DEFAULT_SEED, SLA_HOURS, fmt, utcnow
from .db import connect, drop_all, init_schema

MERCHANTS = [
    ("M-001", "Northwind Outfitters (Demo)", "Enterprise", "North America", "Sports & Outdoors"),
    ("M-002", "Lumen Electronics Hub (Demo)", "Enterprise", "Europe", "Consumer Electronics"),
    ("M-003", "Harbor & Pine Home (Demo)", "Growth", "North America", "Home & Kitchen"),
    ("M-004", "Kestrel Fashion Co (Demo)", "Growth", "Europe", "Apparel"),
    ("M-005", "Meridian Beauty Lab (Demo)", "Growth", "Asia Pacific", "Beauty"),
    ("M-006", "Bluefin Gadget Market (Demo)", "Standard", "Asia Pacific", "Consumer Electronics"),
    ("M-007", "Copperleaf Living (Demo)", "Standard", "North America", "Home & Kitchen"),
    ("M-008", "Summit Trail Supply (Demo)", "Standard", "Europe", "Sports & Outdoors"),
    ("M-009", "Aurora Wearables (Demo)", "Growth", "Asia Pacific", "Apparel"),
    ("M-010", "Redwood Pet Goods (Demo)", "Standard", "North America", "Pet Supplies"),
    ("M-011", "Orchid Skincare Studio (Demo)", "Standard", "Europe", "Beauty"),
    ("M-012", "Granite Tools & Hardware (Demo)", "Enterprise", "North America", "Home & Kitchen"),
]

# category -> (brands, nouns, variants, median price, log-sigma)
CATEGORIES: dict[str, tuple[list[str], list[str], list[str], float, float]] = {
    "Wireless Headphones": (["Auralis", "Sonex", "Pulsar", "Echoline"], ["Wireless Headphones", "ANC Headphones", "Earbuds"], ["Black", "White", "Silver", "Navy"], 129.0, 0.30),
    "Smartphones": (["Novatek", "Zenith", "Orbix"], ["Smartphone", "5G Phone"], ["128GB", "256GB", "512GB"], 599.0, 0.35),
    "Laptops": (["Corebook", "Vantix", "Lumio"], ["Laptop 14in", "Laptop 15in", "Ultrabook"], ["8GB/256GB", "16GB/512GB", "32GB/1TB"], 899.0, 0.32),
    "Running Shoes": (["Strider", "Apex", "Trailmark", "Velo"], ["Running Shoes", "Trail Runners", "Racing Flats"], ["Size 8", "Size 9", "Size 10", "Size 11"], 94.0, 0.25),
    "Camping Tents": (["Summitline", "Ridgewalk", "Cairn"], ["2-Person Tent", "4-Person Tent", "Backpacking Tent"], ["Green", "Orange", "Grey"], 219.0, 0.30),
    "Cookware Sets": (["Hearthstone", "Maison", "Copperleaf"], ["Cookware Set 10pc", "Nonstick Pan Set", "Dutch Oven"], ["Black", "Red", "Steel"], 119.0, 0.30),
    "Blenders": (["Whirlwind", "Vitalmix", "Pureblend"], ["Countertop Blender", "Personal Blender"], ["600W", "900W", "1200W"], 79.0, 0.28),
    "Skincare Serums": (["Lumea", "Dermique", "Orchid"], ["Vitamin C Serum", "Hyaluronic Serum", "Retinol Serum"], ["30ml", "50ml"], 34.0, 0.25),
    "Denim Jeans": (["Kestrel", "Ironwood", "Bluebird"], ["Slim Fit Jeans", "Straight Jeans", "Relaxed Jeans"], ["W30", "W32", "W34", "W36"], 68.0, 0.22),
    "Smart Watches": (["Aurora", "Chronos", "Tempo"], ["Smart Watch", "Fitness Tracker"], ["Black", "Rose", "Graphite"], 189.0, 0.32),
    "Dog Food": (["Redwood", "Pawsome", "Meadow"], ["Dry Dog Food", "Grain-Free Dog Food"], ["5kg", "10kg", "15kg"], 46.0, 0.25),
    "Power Tools": (["Granite", "Forgeline", "Ironclad"], ["Cordless Drill", "Impact Driver", "Circular Saw"], ["18V", "20V", "Kit"], 139.0, 0.30),
}

VERTICAL_CATEGORIES = {
    "Sports & Outdoors": ["Running Shoes", "Camping Tents", "Smart Watches"],
    "Consumer Electronics": ["Wireless Headphones", "Smartphones", "Laptops", "Smart Watches"],
    "Home & Kitchen": ["Cookware Sets", "Blenders", "Power Tools"],
    "Apparel": ["Denim Jeans", "Running Shoes", "Smart Watches"],
    "Beauty": ["Skincare Serums"],
    "Pet Supplies": ["Dog Food"],
}

ASSIGNEES = ["Avery Kim", "Jordan Lee", "Morgan Patel", "Riley Chen", "Casey Novak", "Taylor Brooks", "Sam Okafor", "Drew Alvarez"]
PLACEHOLDER_IMAGE = "https://images.example-demo.com/placeholder/no-image.png"
IMAGE_BASE = "https://images.example-demo.com"

# --- Request templates (subject, body). {n} items, {cat} category ------------------------------
REQUEST_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "Product Onboarding": [
        ("Onboard new {cat} products", "We would like to add {n} new products in {cat} to our catalog. Spreadsheet attached with titles, SKUs and prices."),
        ("New collection launch: {cat}", "We are launching a new collection and need {n} new listings created for {cat} before our launch."),
        ("Catalog setup for {n} new SKUs", "Please help with catalog setup for {n} new SKUs in {cat}. We need onboarding guidance for required attributes."),
        ("Add new items to our store", "Please add new items for {cat}. We have {n} new products ready to be created."),
    ],
    "Price Update": [
        ("Update prices for {cat}", "Please update the price for {n} items in {cat}. New pricing sheet is attached."),
        ("Wrong price displayed on listings", "Several listings show a wrong price compared to our MSRP, around {n} products in {cat}."),
        ("Apply discount to {cat}", "We are running a promotion and need a discount applied to {n} products in {cat}."),
        ("Sale price change request", "Need a sale price change on {n} SKUs. Markdown starts next week."),
    ],
    "Image Update": [
        ("Product images missing for {cat}", "About {n} listings in {cat} have no image or show a placeholder. New photos are ready to upload."),
        ("Broken image links", "We found broken image links on {n} products. The thumbnail does not render on the storefront."),
        ("Replace product photos", "Please replace the pictures for {n} products in {cat} with the updated gallery images."),
        ("Update gallery images", "We refreshed our photography for {cat}. Please update the images on {n} listings."),
    ],
    "Category Correction": [
        ("Miscategorized {cat} products", "{n} products are miscategorized and appear in the wrong category. They should be in {cat}."),
        ("Fix taxonomy for {cat}", "Our items are mapped to the wrong department in the taxonomy. Please correct the category for {n} products."),
        ("Wrong category on listings", "Listings for {cat} are showing in the wrong category, roughly {n} of them."),
        ("Category mapping review", "Please review the attribute mapping and category for {n} {cat} items."),
    ],
    "Listing Takedown": [
        ("Please take down discontinued {cat} items", "These {n} products are discontinued. Please remove the listings from the storefront."),
        ("Delist products - recall notice", "A manufacturer recall was announced. Please delist {n} products in {cat} right away."),
        ("Counterfeit listing report", "We found a counterfeit copy of our product listed in {cat}. Please take down the listing."),
        ("Remove listings for {cat}", "Please remove {n} listings that we no longer sell. They can be archived."),
    ],
    "Bulk Upload Issue": [
        ("CSV upload failing", "Our bulk CSV upload fails with validation errors. {n} rows rejected out of the last file."),
        ("Feed sync error", "The product feed sync failed overnight and {n} products were not imported."),
        ("Bulk import stuck", "The bulk upload for {cat} has been processing for hours. Import never completed for {n} rows."),
        ("Rows failed during upload", "Rows failed validation during upload for {n} SKUs. Error message is unclear."),
    ],
    "Account Access": [
        ("Cannot log in to merchant portal", "Our team is locked out of the portal. The password reset email never arrives."),
        ("Need permissions for new team member", "Please grant access and the right user role to a new colleague on our account."),
        ("2FA problem on our account", "Two-factor codes are not working for our admin user. We cannot access the dashboard."),
        ("SSO login failing", "Our SSO login fails with an error. Several users cannot sign in."),
    ],
    "General Inquiry": [
        ("Question about reporting", "Could you explain how the monthly performance statement is calculated?"),
        ("Feedback on the portal", "We have some feedback about the portal navigation and would like to schedule a call."),
        ("Partnership question", "Who is the right contact to discuss upcoming seasonal programs?"),
    ],
}
URGENCY_SUFFIXES = [
    "This is urgent, we have a campaign going live today.",
    "Please handle this asap, it is blocking our sales.",
    "Our deadline is end of day.",
    "Customers are complaining and we are unable to sell.",
]
TYPE_WEIGHTS = {
    "Product Onboarding": 18, "Price Update": 20, "Image Update": 15, "Category Correction": 11,
    "Listing Takedown": 8, "Bulk Upload Issue": 13, "Account Access": 8, "General Inquiry": 7,
}
# mean hours of hands-on work (In Progress -> In Review), probability of rework
EFFORT_HOURS = {
    "Product Onboarding": 26, "Price Update": 6, "Image Update": 12, "Category Correction": 16,
    "Listing Takedown": 4, "Bulk Upload Issue": 22, "Account Access": 3, "General Inquiry": 8,
}
REWORK_PROB = {
    "Product Onboarding": 0.22, "Price Update": 0.10, "Image Update": 0.14, "Category Correction": 0.30,
    "Listing Takedown": 0.05, "Bulk Upload Issue": 0.26, "Account Access": 0.04, "General Inquiry": 0.06,
}
PRIORITY_SPEED = {"P1": 0.28, "P2": 0.9, "P3": 1.2, "P4": 1.8}
TRIAGE_HOURS = {"P1": 0.5, "P2": 1.8, "P3": 5.0, "P4": 12.0}


@dataclass
class SeedStats:
    seed: int
    as_of: str
    merchants: int = 0
    products: int = 0
    requests: int = 0
    events: int = 0
    injected_duplicate_skus: set[tuple[str, str]] = field(default_factory=set)
    injected_price_anomaly_skus: set[tuple[str, str]] = field(default_factory=set)
    injected_missing_image_skus: set[tuple[str, str]] = field(default_factory=set)
    labeled_requests: list[tuple[str, str, str]] = field(default_factory=list)  # subject, body, true type


def _lognormal(rng: random.Random, mean: float, sigma: float) -> float:
    """Lognormal sample whose *mean* equals `mean`."""
    mu = math.log(max(mean, 1e-6)) - sigma**2 / 2
    return rng.lognormvariate(mu, sigma)


def _title_variation(rng: random.Random, title: str) -> tuple[str, str]:
    """Return (variant_title, kind). 'exact' normalises to the same text; 'near' is a small edit."""
    roll = rng.random()
    if roll < 0.30:
        return title.upper(), "exact"
    if roll < 0.55:
        return title.replace(" ", "  ", 1).replace("-", " - "), "exact"
    if roll < 0.70:
        return f"{title}!", "exact"
    if roll < 0.85:
        words = title.split()
        i = rng.randrange(1, len(words)) if len(words) > 1 else 0
        w = words[i]
        words[i] = w[:-1] if len(w) > 3 else w
        return " ".join(words), "near"
    return title.replace("Wireless", "Wirelss").replace("Running", "Runing").replace("Smart", "Smrt") + " ", "near"


def generate_products(rng: random.Random, as_of: datetime, stats: SeedStats, per_merchant: int = 150) -> list[dict]:
    rows: list[dict] = []
    for mid, _name, _tier, _region, vertical in MERCHANTS:
        cats = VERTICAL_CATEGORIES[vertical]
        seen_titles: set[str] = set()
        for i in range(per_merchant):
            cat = rng.choice(cats)
            brands, nouns, variants, median, sigma = CATEGORIES[cat]
            brand = rng.choice(brands)
            title = f"{brand} {rng.choice(['Pro', 'Max', 'Lite', 'Plus', 'Air'])} {rng.randint(100, 990)} {rng.choice(nouns)} - {rng.choice(variants)}"
            if title in seen_titles:
                continue
            seen_titles.add(title)
            created = as_of - timedelta(days=rng.uniform(5, 400))
            status = rng.choices(["active", "draft", "archived"], [90, 7, 3])[0]
            price = round(median * rng.lognormvariate(0, sigma), 2)
            sku = f"{mid[2:]}-{cat[:3].upper()}-{i + 1:04d}"
            rows.append(
                {
                    "merchant_id": mid, "sku": sku, "title": title, "brand": brand, "category": cat, "price": price,
                    "currency": "USD", "image_url": f"{IMAGE_BASE}/{mid}/{sku}.jpg",
                    "description": f"{title}. Synthetic demo listing from the {cat} category with standard warranty and free returns.",
                    "status": status, "created_at": fmt(created), "updated_at": fmt(created + timedelta(days=rng.uniform(0, 4))),
                }
            )

    # --- inject known defects (ground truth kept in stats for test verification) ---
    n = len(rows)
    eligible = [r for r in rows if r["status"] != "archived"]
    for r in rng.sample(eligible, int(n * 0.04)):
        factor = rng.choice([8, 10, 12, 15, 1 / 8, 1 / 10, 1 / 12])
        r["price"] = round(r["price"] * factor, 2)
        stats.injected_price_anomaly_skus.add((r["merchant_id"], r["sku"]))
    for r in rng.sample([r for r in rows if r["status"] == "active"], 3):
        r["price"] = 0.0
        stats.injected_price_anomaly_skus.add((r["merchant_id"], r["sku"]))
    for r in rng.sample(rows, int(n * 0.08)):
        r["image_url"] = rng.choice([None, "", PLACEHOLDER_IMAGE, "not-a-url", "N/A"])
        stats.injected_missing_image_skus.add((r["merchant_id"], r["sku"]))
    for r in rng.sample(rows, int(n * 0.05)):
        r["description"] = rng.choice([None, "", "TBD"])
    for r in rng.sample(rows, int(n * 0.02)):
        r["brand"] = None

    originals = [r for r in rows if r["status"] == "active"]
    dups: list[dict] = []
    for k, src in enumerate(rng.sample(originals, int(n * 0.035))):
        title, _kind = _title_variation(rng, src["title"])
        copy = dict(src)
        copy.update(
            {
                "sku": f"{src['sku']}-D{k + 1}", "title": title,
                "price": round(src["price"] * rng.uniform(0.97, 1.03), 2),
                "created_at": fmt(as_of - timedelta(days=rng.uniform(1, 60))),
            }
        )
        dups.append(copy)
        stats.injected_duplicate_skus.add((copy["merchant_id"], copy["sku"]))
    rows.extend(dups)
    return rows


def _request_text(rng: random.Random, rtype: str, cat: str, n: int, urgent: bool) -> tuple[str, str]:
    subject_tpl, body_tpl = rng.choice(REQUEST_TEMPLATES[rtype])
    subject, body = subject_tpl.format(n=n, cat=cat), body_tpl.format(n=n, cat=cat)
    if urgent:
        body = f"{body} {rng.choice(URGENCY_SUFFIXES)}"
    return subject, body


def _simulate_lifecycle(rng: random.Random, rtype: str, priority: str, created: datetime, as_of: datetime, assignee: str):
    """Walk a request through the lifecycle until `as_of`. Returns (status, events, rework, first_resp, resolved)."""
    events: list[tuple[str, str | None, str, str, str, datetime]] = []  # (event_type, from, to, actor, note, ts)
    t = created
    events.append(("created", None, "New", "merchant", "Request submitted", t))
    status, rework, first_resp, resolved = "New", 0, None, None
    speed = PRIORITY_SPEED[priority]
    drag = 3.0 if rng.random() < 0.08 else 1.0  # ~10% of requests hit a slow lane (backlog, escalation)

    def step(to: str, hours: float, note: str = "", etype: str = "status_change") -> bool:
        nonlocal t, status, first_resp, resolved
        nt = t + timedelta(hours=hours * drag)
        if nt > as_of:
            return False
        events.append((etype, status, to, assignee if to != "New" else "merchant", note, nt))
        t, status = nt, to
        if to == "Triaged":
            first_resp = nt
        if to == "Resolved":
            resolved = nt
        return True

    if not step("Triaged", _lognormal(rng, TRIAGE_HOURS[priority], 0.7), "Triaged and assigned"):
        return status, events, rework, first_resp, resolved
    if rng.random() < 0.03:
        step("Rejected", _lognormal(rng, 2.0, 0.5), "Out of scope / duplicate request")
        return status, events, rework, first_resp, resolved
    effort = EFFORT_HOURS[rtype] * speed
    if not step("In Progress", _lognormal(rng, 2.5 * speed + 0.5, 0.6), "Work started"):
        return status, events, rework, first_resp, resolved
    if rng.random() < 0.20 and rtype not in ("Listing Takedown", "Account Access"):
        if not step("Waiting on Merchant", _lognormal(rng, 3.0 * speed + 0.5, 0.5), "Clarification requested"):
            return status, events, rework, first_resp, resolved
        if not step("In Progress", _lognormal(rng, 12.0 * speed + 2, 0.7), "Merchant replied"):
            return status, events, rework, first_resp, resolved
    if not step("In Review", _lognormal(rng, effort, 0.7), "Submitted for QA review"):
        return status, events, rework, first_resp, resolved
    while rng.random() < REWORK_PROB[rtype] and rework < 3:
        if not step("In Progress", _lognormal(rng, 2.0 * speed + 0.5, 0.5), "QA found issues - rework", "rework"):
            return status, events, rework, first_resp, resolved
        rework += 1
        if not step("In Review", _lognormal(rng, effort * 0.4, 0.6), "Resubmitted for review"):
            return status, events, rework, first_resp, resolved
    if not step("Resolved", _lognormal(rng, 2.5 * speed + 0.5, 0.6), "Fix verified"):
        return status, events, rework, first_resp, resolved
    step("Closed", _lognormal(rng, 20.0, 0.6), "Confirmed by merchant / auto-closed")
    return status, events, rework, first_resp, resolved


def generate_requests(rng: random.Random, as_of: datetime, stats: SeedStats, count: int = 700, days: int = 120):
    types = list(TYPE_WEIGHTS)
    weights = [TYPE_WEIGHTS[t] for t in types]
    tiers = {m[0]: m[2] for m in MERCHANTS}
    merchant_weights = [{"Enterprise": 3, "Growth": 2, "Standard": 1}[m[2]] for m in MERCHANTS]
    requests, events = [], []
    created_times = sorted(as_of - timedelta(hours=days * 24 * rng.random() ** 1.6) for _ in range(count))
    for idx, created in enumerate(created_times, start=1):
        rtype = rng.choices(types, weights)[0]
        merchant = rng.choices(MERCHANTS, merchant_weights)[0]
        mid = merchant[0]
        cat = rng.choice(VERTICAL_CATEGORIES[merchant[4]])
        n = int(min(5000, _lognormal(rng, 80, 1.1))) + 1 if rtype not in ("Account Access", "General Inquiry") else 0
        urgent = rng.random() < 0.22
        subject, body = _request_text(rng, rtype, cat, n, urgent)
        stats.labeled_requests.append((subject, body, rtype))
        priority = recommend_priority(subject, body, rtype, tiers[mid], n).priority
        assignee = rng.choice(ASSIGNEES)
        status, evs, rework, first_resp, resolved = _simulate_lifecycle(rng, rtype, priority, created, as_of, assignee)
        rid = f"REQ-{idx:05d}"
        requests.append(
            {
                "request_id": rid, "merchant_id": mid, "subject": subject, "description": body, "request_type": rtype,
                "priority": priority, "status": status, "channel": rng.choices(["Portal", "Email", "Partner Manager", "API"], [45, 30, 15, 10])[0],
                "assignee": None if status == "New" else assignee, "affected_items": n, "created_at": fmt(created),
                "due_at": fmt(created + timedelta(hours=SLA_HOURS[priority])),
                "first_response_at": fmt(first_resp) if first_resp else None,
                "resolved_at": fmt(resolved) if resolved else None, "rework_count": rework,
                "classification_source": "rules", "classification_confidence": None,
            }
        )
        for etype, frm, to, actor, note, ts in evs:
            events.append((rid, etype, frm, to, actor, note, fmt(ts)))
    return requests, events


def seed_database(path=None, seed: int = DEFAULT_SEED, as_of: datetime | None = None, reset: bool = True) -> SeedStats:
    """Create schema and populate it. Deterministic for a given (seed, as_of)."""
    as_of = (as_of or utcnow()).replace(microsecond=0)
    rng = random.Random(seed)
    stats = SeedStats(seed=seed, as_of=fmt(as_of))
    products = generate_products(rng, as_of, stats)
    requests, events = generate_requests(rng, as_of, stats)

    # classify seeded requests with the deterministic model so the stored metadata is real
    from .classifier import classify_rules

    for r in requests:
        c = classify_rules(r["subject"], r["description"])
        r["classification_confidence"] = c.confidence

    with connect(path) as conn:
        if reset:
            drop_all(conn)
        init_schema(conn)
        conn.executemany(
            "INSERT INTO merchants VALUES (?,?,?,?,?,?)",
            [(m[0], m[1], m[2], m[3], m[4], fmt(as_of - timedelta(days=500))) for m in MERCHANTS],
        )
        cols = ["merchant_id", "sku", "title", "brand", "category", "price", "currency", "image_url", "description", "status", "created_at", "updated_at"]
        conn.executemany(
            f"INSERT INTO products ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [tuple(p[c] for c in cols) for p in products],
        )
        rcols = list(requests[0].keys())
        conn.executemany(
            f"INSERT INTO merchant_requests ({','.join(rcols)}) VALUES ({','.join('?' * len(rcols))})",
            [tuple(r[c] for c in rcols) for r in requests],
        )
        conn.executemany(
            "INSERT INTO request_events (request_id,event_type,from_status,to_status,actor,note,created_at) VALUES (?,?,?,?,?,?,?)",
            events,
        )
        conn.execute("INSERT INTO app_meta VALUES ('seeded_as_of', ?)", (fmt(as_of),))
    stats.merchants, stats.products, stats.requests, stats.events = len(MERCHANTS), len(products), len(requests), len(events)
    return stats


DEMO_REFRESH_DAYS = 3


def ensure_demo_data(path=None, now: datetime | None = None) -> bool:
    """Seed automatically when the database is missing, empty or stale (> DEMO_REFRESH_DAYS old).

    Staleness matters because open requests are measured against the wall clock: without a refresh
    a long-running demo would drift towards "everything is breaching SLA". Returns True if seeded.
    """
    from .db import is_seeded

    now = now or utcnow()
    with connect(path) as conn:
        init_schema(conn)
        if is_seeded(conn):
            row = conn.execute("SELECT value FROM app_meta WHERE key = 'seeded_as_of'").fetchone()
            try:
                age = now - datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S") if row else None
            except ValueError:
                age = None
            if age is not None and age <= timedelta(days=DEMO_REFRESH_DAYS):
                return False
    seed_database(path)
    return True
