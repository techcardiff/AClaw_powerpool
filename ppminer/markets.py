"""Per-region simulation defaults (USA plus the markets the scheduler explores)."""
from __future__ import annotations

# BRAIN's main universe per region (delay 1)
UNIVERSE = {
    "USA": "TOP3000", "EUR": "TOP2500", "ASI": "MINVOL1M", "GLB": "TOP3000", "CHN": "TOP2000U",
    "KOR": "TOP600", "JPN": "TOP1600", "HKG": "TOP800", "TWN": "TOP500", "AMR": "TOP600", "IND": "TOP500",
}
# multi-country regions: neutralize within country-level industries
NEUTRALIZATION = {"EUR": "SUBINDUSTRY", "ASI": "SUBINDUSTRY", "GLB": "SUBINDUSTRY"}


def universe(region: str | None) -> str:
    return UNIVERSE.get((region or "USA").upper(), "TOP3000")


def neutralization(region: str | None) -> str:
    return NEUTRALIZATION.get((region or "USA").upper(), "SUBINDUSTRY")
