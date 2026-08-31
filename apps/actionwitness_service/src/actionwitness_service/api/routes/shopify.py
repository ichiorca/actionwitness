"""Shopify development-store pairing and bridge routes.

Endpoints (spec v1.9 §15.7, tier T3) — NOT implemented yet:
    (pairing create/redeem, observation submit, verify — per §15.7 / FR-110..119)
"""

from fastapi import APIRouter

router = APIRouter()
