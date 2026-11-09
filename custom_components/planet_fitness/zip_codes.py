"""US ZIP code locations, from the Census Bureau's 2025 ZCTA gazetteer (public domain)."""

from __future__ import annotations

import gzip
from pathlib import Path
import re

_DATA = Path(__file__).with_name("zip_codes.tsv.gz")  # zip, latitude, longitude; sorted by zip
_ZIP = re.compile(r"([0-9]{5})(?:[- ]?[0-9]{4})?")


def parse_zip(text: str) -> str | None:
    """The 5-digit ZIP code in `text` (ZIP+4 allowed), or None."""
    match = _ZIP.fullmatch(text.strip())
    return match[1] if match else None


def zip_location(zip_code: str) -> tuple[float, float] | None:
    """(latitude, longitude) of a 5-digit ZIP code, or None if unknown. Blocking I/O.

    PO box and single-company ZIP codes have no area of their own, so they get the
    numerically closest ZIP code that shares their first three digits (same mail hub).
    """
    prefix, number = zip_code[:3], int(zip_code)
    closest: tuple[int, float, float] | None = None
    with gzip.open(_DATA, "rt", encoding="ascii") as rows:
        for row in rows:
            code, lat, lon = row.split("\t")
            if code == zip_code:
                return float(lat), float(lon)
            if code[:3] == prefix:
                gap = abs(int(code) - number)
                if closest is None or gap < closest[0]:
                    closest = (gap, float(lat), float(lon))
            elif code[:3] > prefix:
                break
    return (closest[1], closest[2]) if closest else None
