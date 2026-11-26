"""Synthetic merchant file workload with KNOWN injected defects (ground truth), used to measure automation.

Everything is generated from a seed: fictional merchants and products, no real data. Files are named
`M-###_catalog_<nn>.csv|xlsx` so the automation can route each file to its merchant.
"""
from __future__ import annotations

import io
import random
from dataclasses import dataclass, field

import pandas as pd

from .excel import to_xlsx_bytes
from .seed import CATEGORIES, MERCHANTS, PLACEHOLDER_IMAGE, VERTICAL_CATEGORIES, _title_variation

COLUMNS = ["sku", "title", "brand", "category", "price", "currency", "image_url", "description", "status"]
WORKLOAD_SEED = 777


@dataclass
class Workload:
    files: list[tuple[str, bytes]]
    rows_total: int
    truth: dict[str, set[tuple[str, str]]] = field(default_factory=dict)  # kind -> {(merchant_id, sku)}
    dup_pairs: set[tuple[str, str, str]] = field(default_factory=set)  # (merchant_id, original_sku, copy_sku)
    invalid_rows: int = 0
    raw_frames: dict[str, pd.DataFrame] = field(default_factory=dict)  # filename -> rows as written (all strings)

    @property
    def injected_counts(self) -> dict[str, int]:
        return {"duplicate": len(self.dup_pairs), **{k: len(v) for k, v in self.truth.items()}, "invalid_rows": self.invalid_rows}

    def combined_raw(self) -> pd.DataFrame:
        frames = []
        for name, df in self.raw_frames.items():
            frames.append(df.assign(merchant_id=name[:5]))
        return pd.concat(frames, ignore_index=True)


def generate_workload(
    n_files: int = 10, rows_per_file: int = 500, seed: int = WORKLOAD_SEED, xlsx_share: float = 0.5,
    duplicate_rate: float = 0.03, price_rate: float = 0.04, image_rate: float = 0.08, invalid_rate: float = 0.03,
) -> Workload:
    rng = random.Random(seed)
    truth: dict[str, set[tuple[str, str]]] = {"price_anomaly": set(), "missing_image": set()}
    dup_pairs: set[tuple[str, str, str]] = set()
    seen_titles: dict[str, set[str]] = {}
    files, frames, invalid_total, rows_total = [], {}, 0, 0
    xlsx_files = set(rng.sample(range(n_files), round(n_files * xlsx_share))) if n_files else set()
    per_merchant_count: dict[str, int] = {}
    for i in range(n_files):
        mid, _name, _tier, _region, vertical = MERCHANTS[i % len(MERCHANTS)]
        k = per_merchant_count[mid] = per_merchant_count.get(mid, 0) + 1
        cats = VERTICAL_CATEGORIES[vertical]
        titles = seen_titles.setdefault(mid, set())
        n_base = int(rows_per_file / (1 + duplicate_rate))
        rows: list[dict] = []
        for j in range(n_base):
            cat = rng.choice(cats)
            brands, nouns, variants, median, sigma = CATEGORIES[cat]
            brand = rng.choice(brands)
            for _ in range(50):
                title = f"{brand} {rng.choice(['Pro', 'Max', 'Lite', 'Plus', 'Air'])} {rng.randint(100, 990)} {rng.choice(nouns)} - {rng.choice(variants)}"
                if title not in titles:
                    break
            titles.add(title)
            sku = f"{mid[2:]}-W{k}{i:02d}-{j + 1:05d}"
            rows.append({
                "sku": sku, "title": title, "brand": brand, "category": cat, "price": f"{median * rng.lognormvariate(0, sigma):.2f}", "currency": "USD",
                "image_url": f"https://images.example-demo.com/{mid}/{sku}.jpg",
                "description": "" if rng.random() < 0.05 else f"{title}. Synthetic demo listing with standard warranty and free returns.",
                "status": "active",
            })
        idx = list(range(len(rows)))
        rng.shuffle(idx)
        take = lambda frac: [idx.pop() for _ in range(min(len(idx), int(len(rows) * frac)))]  # noqa: E731 - disjoint defect groups
        invalid_idx, price_idx, image_idx, dup_src = take(invalid_rate), take(price_rate), take(image_rate), take(duplicate_rate)
        for r_i in price_idx:
            r = rows[r_i]
            r["price"] = f"{float(r['price']) * rng.choice([8, 10, 12, 15, 1 / 8, 1 / 10, 1 / 12]):.2f}"
            truth["price_anomaly"].add((mid, r["sku"]))
        for r_i in image_idx:
            r = rows[r_i]
            r["image_url"] = rng.choice(["", PLACEHOLDER_IMAGE, "N/A", "not-a-url"])
            truth["missing_image"].add((mid, r["sku"]))
        n_invalid = 0
        for r_i in invalid_idx:
            r = rows[r_i]
            kind = rng.choice(["blank_sku", "bad_price", "negative_price", "short_title"])
            if kind == "blank_sku":
                r["sku"] = ""
            elif kind == "bad_price":
                r["price"] = rng.choice(["abc", "N/A", "--"])
            elif kind == "negative_price":
                r["price"] = "-5.00"
            else:
                r["title"] = "X"
            n_invalid += 1
        for d_i, r_i in enumerate(dup_src):
            src = rows[r_i]
            title, _kind = _title_variation(rng, src["title"])
            rows.append({**src, "sku": f"{src['sku']}-D{d_i + 1}", "title": title, "price": f"{float(src['price']) * rng.uniform(0.97, 1.03):.2f}"})
            dup_pairs.add((mid, src["sku"], rows[-1]["sku"]))
        rng.shuffle(rows)
        df = pd.DataFrame(rows, columns=COLUMNS)
        name = f"{mid}_catalog_{i:02d}"
        if i in xlsx_files:
            data, name = to_xlsx_bytes({"Catalog": df}), name + ".xlsx"
        else:
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            data, name = buf.getvalue().encode("utf-8"), name + ".csv"
        files.append((name, data))
        frames[name] = df
        invalid_total += n_invalid
        rows_total += len(df)
    # defect groups are disjoint, so an invalid (never imported) row is never also a duplicate/price/image target
    return Workload(files, rows_total, truth, dup_pairs, invalid_total, frames)
