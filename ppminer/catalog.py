"""Data-field catalog built from the per-region brain_meta*.sqlite snapshots.

Maps field id -> (category, dataset id, type) per region, with a cross-region
fallback because many fields share ids across regions.
"""
from __future__ import annotations

import glob
import os
import sqlite3
from dataclasses import dataclass

# BRAIN category names -> the short ids used in Power Pool theme strings
CATEGORY_ID = {
    "price volume": "pv", "fundamental": "fundamental", "analyst": "analyst",
    "model": "model", "news": "news", "option": "option", "sentiment": "sentiment",
    "social media": "socialmedia", "earnings": "earnings", "insiders": "insiders",
    "institutions": "institutions", "macro": "macro", "risk": "risk",
    "short interest": "shortinterest", "other": "other", "imbalance": "imbalance",
}


@dataclass(frozen=True)
class FieldInfo:
    id: str
    category: str
    dataset: str          # dataset id, e.g. analyst4
    type: str             # MATRIX | VECTOR | GROUP ...
    region: str
    description: str = ""


class Catalog:
    def __init__(self, catalog_dir: str):
        self.by_region: dict[str, dict[str, FieldInfo]] = {}
        self.any_region: dict[str, FieldInfo] = {}
        self.operators: set[str] = set()
        self.op_docs: dict[str, str] = {}       # operator -> "signature: BRAIN's description"
        for path in sorted(glob.glob(os.path.join(catalog_dir, "brain_meta*.sqlite"))):
            try:
                self._load(path)
            except sqlite3.DatabaseError as e:
                print(f"catalog: skipping {os.path.basename(path)}: {e}")

    def _load(self, path: str) -> None:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        ds_id = {name: did for did, name in c.execute("select id, name from datasets")}
        for name, definition, desc in c.execute("select name, definition, description from operators"):
            self.operators.add(name)
            self.op_docs.setdefault(name, f"{' '.join((definition or name).split())}: {' '.join((desc or '').split())}")
        for fid, ftype, dname, cname, region, desc in c.execute(
                "select id, type, dataset_name, category_name, region, description from datafields"):
            cat = CATEGORY_ID.get((cname or "").lower(), (cname or "unknown").lower().replace(" ", ""))
            info = FieldInfo(fid, cat, ds_id.get(dname, dname or "?"), ftype or "?", region or "?", desc or "")
            self.by_region.setdefault(info.region, {})[fid] = info
            self.any_region.setdefault(fid, info)
        c.close()

    def add_account_fields(self, conn, account: str) -> int:
        """Add fields synced for an account (pp.db account_fields); they cover datasets
        the old competition catalog never had."""
        n = 0
        for fid, region, ds, cat, ftype, desc in conn.execute(
                "select field, region, dataset, category, type, description from account_fields where account=?",
                (account,)):
            info = FieldInfo(fid, cat or "unknown", ds, ftype or "?", region, desc or "")
            self.by_region.setdefault(region, {})[fid] = info
            self.any_region.setdefault(fid, info)
            n += 1
        return n

    def get(self, fid: str, region: str | None = None) -> FieldInfo | None:
        if region and fid in self.by_region.get(region, {}):
            return self.by_region[region][fid]
        return self.any_region.get(fid)

    def regions(self) -> list[str]:
        return sorted(self.by_region)
