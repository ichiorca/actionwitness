"""Workspace and capability routes.

Endpoints (spec v1.8 §15.1, tier T1) — NOT implemented yet:
    GET  /workspace ; POST /workspace/reset ; PUT /workspace/failure-profile
"""

from fastapi import APIRouter

router = APIRouter()
