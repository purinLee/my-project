"""Hand-written, differently-phrased requests (NOT drawn from the seed templates) as a harsher accuracy check."""
from catalog_intel.classifier import classify_rules

HELD_OUT = [
    ("Our new spring line is ready", "We have 40 new SKUs for spring and need help getting them listed. Onboarding docs please.", "Product Onboarding"),
    ("Need to get these items live", "Add new products from the attached sheet to our storefront.", "Product Onboarding"),
    ("Prices are off after the weekend sale", "The sale price did not revert; please correct the pricing on our running shoes.", "Price Update"),
    ("MSRP change", "Manufacturer raised MSRP by 5%, please adjust all prices accordingly.", "Price Update"),
    ("Pictures look blurry", "The product photos for our tents are low quality, can we swap in new pictures?", "Image Update"),
    ("Thumbnails not showing", "Thumbnail is broken for several of our listings.", "Image Update"),
    ("Laptops listed under accessories", "These are miscategorized and should be moved to the laptops category.", "Category Correction"),
    ("Taxonomy question", "Our items sit in the wrong category after the taxonomy update.", "Category Correction"),
    ("Please delist the old model", "This model is discontinued, remove the listing today.", "Listing Takedown"),
    ("Counterfeit seller", "Someone is selling a counterfeit of our product, take down the listing.", "Listing Takedown"),
    ("Feed keeps failing", "The nightly feed import errors out and 200 rows failed.", "Bulk Upload Issue"),
    ("Bulk upload errors", "Our CSV has validation errors we do not understand.", "Bulk Upload Issue"),
    ("Locked out", "None of us can log in since the password policy changed.", "Account Access"),
    ("Add a user", "Please grant permissions to our new analyst on the account.", "Account Access"),
    ("Hello team", "Wanted to say thanks for the great quarter.", "General Inquiry"),
    ("Question", "Is there a webinar next month?", "General Inquiry"),
]


def test_held_out_accuracy():
    correct = [classify_rules(s, b).request_type == t for s, b, t in HELD_OUT]
    accuracy = sum(correct) / len(correct)
    wrong = [(s, classify_rules(s, b).request_type) for (s, b, t), ok in zip(HELD_OUT, correct) if not ok]
    assert accuracy >= 0.85, f"held-out accuracy {accuracy:.0%}; wrong: {wrong}"
