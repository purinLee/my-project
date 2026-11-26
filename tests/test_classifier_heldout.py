"""Hand-written, differently-phrased requests (NOT drawn from the seed templates) as a harsher accuracy check."""
from catalog_intel.classifier import classify_rules
from catalog_intel.evalset import HELD_OUT

def test_held_out_accuracy():
    correct = [classify_rules(s, b).request_type == t for s, b, t in HELD_OUT]
    accuracy = sum(correct) / len(correct)
    wrong = [(s, classify_rules(s, b).request_type) for (s, b, t), ok in zip(HELD_OUT, correct) if not ok]
    assert accuracy >= 0.85, f"held-out accuracy {accuracy:.0%}; wrong: {wrong}"
