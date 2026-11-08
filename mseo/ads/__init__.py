"""Google Ads data layer, audit and action batches."""

from __future__ import annotations

import os
from pathlib import Path

# templates/ads/ in the source checkout; MSEO_TEMPLATES points elsewhere for an installed tool.
TEMPLATES = Path(os.environ.get("MSEO_TEMPLATES") or Path(__file__).resolve().parents[2] / "templates") / "ads"
