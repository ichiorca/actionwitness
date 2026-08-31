"""Contract template and selection routes.

Endpoints (spec v1.8 §15.2, tier T1) — NOT implemented yet:
    GET /contracts/templates ; POST /contracts ; GET /contracts/{id} ; POST /contracts/{id}/select
"""

from fastapi import APIRouter

router = APIRouter()
