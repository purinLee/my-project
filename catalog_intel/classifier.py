"""Request classification and priority recommendation.

Classification is AI-assisted: an optional LLM (see ai.py) is tried first when an API key is
configured. Every failure path falls back to the deterministic keyword-scoring model below, so
the app never depends on an external service.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import PRIORITIES, REQUEST_TYPES

# (pattern, weight). Patterns are matched case-insensitively on the combined subject + body.
RULES: dict[str, list[tuple[str, float]]] = {
    "Product Onboarding": [
        (r"\bonboard(ing)?\b", 3), (r"\bnew (product|products|item|items|sku|skus|listing|listings)\b", 3),
        (r"\badd (new )?(product|products|item|items|sku|skus)\b", 3), (r"\blaunch\b", 1.5),
        (r"\bcreate (a )?(listing|listings|product)\b", 2.5), (r"\bnew collection\b", 2), (r"\bcatalog setup\b", 2),
    ],
    "Price Update": [
        (r"\bprice(s)?\b", 2.5), (r"\bpricing\b", 2.5), (r"\bdiscount(s)?\b", 2), (r"\bpromo(tion|tional)?\b", 2),
        (r"\bmarkdown\b", 2), (r"\bsale price\b", 3), (r"\bwrong price\b", 3), (r"\bmsrp\b", 2), (r"\bcurrency\b", 1.5),
    ],
    "Image Update": [
        (r"\bimage(s)?\b", 3), (r"\bphoto(s|graph)?\b", 3), (r"\bpicture(s)?\b", 3), (r"\bthumbnail(s)?\b", 2.5),
        (r"\bbroken (image|link)\b", 3), (r"\bplaceholder\b", 2), (r"\bgallery\b", 1.5),
    ],
    "Category Correction": [
        (r"\bcategor(y|ies|ization|ised|ized)\b", 3), (r"\bmiscategori[sz]ed\b", 4), (r"\btaxonomy\b", 3),
        (r"\bwrong (category|department)\b", 4), (r"\bmove (to|into) (the )?\w+ category\b", 3), (r"\battribute(s)? mapping\b", 2),
    ],
    "Listing Takedown": [
        (r"\btake ?down\b", 4), (r"\bremove (the )?(listing|listings|product|products)\b", 4), (r"\bdelist(ing)?\b", 4),
        (r"\bdiscontinued\b", 3), (r"\brecall(ed)?\b", 3.5), (r"\bcounterfeit\b", 4), (r"\bcopyright\b", 3),
        (r"\bout of stock permanently\b", 3), (r"\barchive\b", 2),
    ],
    "Bulk Upload Issue": [
        (r"\bbulk\b", 3), (r"\bcsv\b", 3), (r"\bupload(s|ed|ing)?\b", 2), (r"\bfeed\b", 2.5), (r"\bimport(s|ed)?\b", 2),
        (r"\bvalidation error(s)?\b", 3), (r"\brows? (rejected|failed)\b", 3.5), (r"\bsync(ing)? (fail|error)\w*\b", 3),
    ],
    "Account Access": [
        (r"\blogin\b", 3), (r"\blog in\b", 3), (r"\bpassword\b", 3), (r"\baccess\b", 2), (r"\bpermission(s)?\b", 3),
        (r"\blocked out\b", 3.5), (r"\b2fa\b|\btwo[- ]factor\b", 3), (r"\buser (role|account)s?\b", 2.5), (r"\bsso\b", 3),
    ],
}

GENERAL = "General Inquiry"
MIN_SCORE = 2.0


@dataclass
class Classification:
    request_type: str
    confidence: float
    source: str  # "rules" | "llm"
    matched: list[str] = field(default_factory=list)
    note: str = ""


def classify_rules(subject: str, description: str = "") -> Classification:
    """Deterministic keyword-scoring classifier. Confidence = share of the top class in total score."""
    text = f"{subject or ''} {description or ''}".lower()
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for rtype, rules in RULES.items():
        for pattern, weight in rules:
            m = re.search(pattern, text)
            if m:
                scores[rtype] = scores.get(rtype, 0.0) + weight
                matched.setdefault(rtype, []).append(m.group(0))
    if not scores or max(scores.values()) < MIN_SCORE:
        return Classification(GENERAL, 0.35, "rules", [], "No strong keyword signal; routed to general inquiry.")
    best = max(scores, key=lambda k: scores[k])
    total = sum(scores.values())
    top = scores[best]
    # Blend dominance with absolute evidence so one weak keyword never yields high confidence.
    confidence = round(min(0.99, 0.5 * (top / total) + 0.5 * min(top / 8.0, 1.0)), 2)
    return Classification(best, confidence, "rules", matched[best])


def classify_request(subject: str, description: str = "", use_llm: bool = True) -> Classification:
    """LLM-assisted classification with deterministic fallback."""
    if use_llm:
        from . import ai  # local import keeps the core usable without the optional layer

        result = ai.classify_with_llm(subject, description)
        if result is not None:
            return result
    return classify_rules(subject, description)


# --- Priority recommendation -----------------------------------------------
TYPE_BASE_SCORE = {
    "Listing Takedown": 3, "Account Access": 3, "Price Update": 2, "Bulk Upload Issue": 2,
    "Product Onboarding": 1, "Image Update": 1, "Category Correction": 1, "General Inquiry": 0,
}
URGENCY_PATTERNS = [
    (r"\b(urgent|urgently|asap|immediately|critical|emergency)\b", 2),
    (r"\b(outage|down|blocked|blocking|cannot|can't|unable|locked out)\b", 2),
    (r"\b(legal|recall(ed)?|counterfeit|copyright|safety|compliance)\b", 2),
    (r"\b(today|tonight|by (eod|end of day)|deadline|launch(es|ing)? (tomorrow|today|this week))\b", 1),
    (r"\b(live (sale|campaign)|black friday|flash sale|campaign)\b", 1),
]
TIER_SCORE = {"Enterprise": 2, "Growth": 1, "Standard": 0}


@dataclass
class PriorityRecommendation:
    priority: str
    score: int
    reasons: list[str]


def recommend_priority(
    subject: str, description: str, request_type: str, merchant_tier: str = "Standard", affected_items: int = 0
) -> PriorityRecommendation:
    text = f"{subject or ''} {description or ''}".lower()
    reasons: list[str] = []
    score = TYPE_BASE_SCORE.get(request_type, 0)
    if score:
        reasons.append(f"Request type '{request_type}' (+{score})")

    urgency = 0
    for pattern, weight in URGENCY_PATTERNS:
        m = re.search(pattern, text)
        if m:
            urgency += weight
            reasons.append(f"Urgency signal '{m.group(0)}' (+{weight})")
    urgency = min(urgency, 3)
    score += urgency

    tier_pts = TIER_SCORE.get(merchant_tier, 0)
    if tier_pts:
        reasons.append(f"{merchant_tier} merchant tier (+{tier_pts})")
    score += tier_pts

    items_pts = 2 if affected_items >= 500 else 1 if affected_items >= 100 else 0
    if items_pts:
        reasons.append(f"{affected_items:,} items affected (+{items_pts})")
    score += items_pts

    priority = "P1" if score >= 7 else "P2" if score >= 5 else "P3" if score >= 3 else "P4"
    assert priority in PRIORITIES
    return PriorityRecommendation(priority, score, reasons or ["No elevating factors"])


__all__ = [
    "Classification", "PriorityRecommendation", "classify_request", "classify_rules", "recommend_priority",
    "REQUEST_TYPES",
]
