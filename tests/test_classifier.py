import pytest

from catalog_intel import ai
from catalog_intel.classifier import classify_request, classify_rules, recommend_priority
from catalog_intel.config import REQUEST_TYPES


@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("Cannot log in to portal", "Password reset email never arrives", "Account Access"),
        ("Update prices", "Please update the price for 40 items", "Price Update"),
        ("Broken image links", "Thumbnail does not render", "Image Update"),
        ("Miscategorized products", "They appear in the wrong category", "Category Correction"),
        ("Delist products", "Manufacturer recall announced, take down listings", "Listing Takedown"),
        ("CSV upload failing", "validation errors, 50 rows rejected", "Bulk Upload Issue"),
        ("Onboard new products", "We want to add 30 new products", "Product Onboarding"),
    ],
)
def test_rules_classify_known_cases(subject, body, expected):
    result = classify_rules(subject, body)
    assert result.request_type == expected
    assert 0.0 < result.confidence <= 0.99
    assert result.source == "rules" and result.matched


def test_unknown_text_falls_back_to_general_inquiry_with_low_confidence():
    r = classify_rules("Hello", "Just saying hi to the team")
    assert r.request_type == "General Inquiry" and r.confidence < 0.5


def test_empty_text_does_not_crash():
    assert classify_rules("", "").request_type == "General Inquiry"


def test_accuracy_on_synthetic_labeled_requests(seeded):
    labeled = seeded[1].labeled_requests
    correct = sum(classify_rules(s, b).request_type == t for s, b, t in labeled)
    accuracy = correct / len(labeled)
    assert len(labeled) >= 500
    assert accuracy >= 0.90, f"rule classifier accuracy {accuracy:.2%}"


def test_classify_request_uses_fallback_when_llm_unconfigured():
    assert not ai.is_configured()
    assert classify_request("Update prices", "price change for 10 items").source == "rules"


def test_llm_failure_falls_back(monkeypatch):
    monkeypatch.setattr(ai, "_secret", lambda name: "fake-key")
    monkeypatch.setattr(ai, "_chat", lambda *a, **k: None)  # simulates network failure
    r = classify_request("Update prices", "price change for 10 items")
    assert r.source == "rules" and r.request_type == "Price Update"


def test_llm_valid_response_is_used(monkeypatch):
    monkeypatch.setattr(ai, "_secret", lambda name: "fake-key")
    monkeypatch.setattr(ai, "_chat", lambda *a, **k: '{"request_type": "Image Update", "confidence": 0.8, "reason": "x"}')
    r = classify_request("whatever", "text")
    assert r.source == "llm" and r.request_type == "Image Update"


@pytest.mark.parametrize(
    "raw",
    [None, "", "not json", '{"request_type": "Hack", "confidence": 0.9}', '{"request_type": "Price Update", "confidence": 7}',
     '{"confidence": 0.5}', '{"request_type": "Price Update", "confidence": "high"}'],
)
def test_llm_output_validation_rejects_bad_payloads(raw):
    assert ai.parse_classification_json(raw) is None


def test_llm_output_validation_accepts_wrapped_json():
    r = ai.parse_classification_json('Sure! {"request_type": "Price Update", "confidence": 0.9} done')
    assert r is not None and r.request_type in REQUEST_TYPES


def test_priority_critical_combination():
    r = recommend_priority("Counterfeit listing - legal notice", "Please take down immediately", "Listing Takedown", "Enterprise", 10)
    assert r.priority == "P1" and r.reasons


def test_priority_low_for_plain_standard_request():
    assert recommend_priority("Question", "How does reporting work?", "General Inquiry", "Standard", 0).priority == "P4"


def test_priority_scales_with_tier_and_volume():
    base = recommend_priority("Update prices", "price change", "Price Update", "Standard", 10)
    big = recommend_priority("Update prices", "price change", "Price Update", "Enterprise", 800)
    order = ["P4", "P3", "P2", "P1"]
    assert order.index(big.priority) > order.index(base.priority)
    assert big.score > base.score
