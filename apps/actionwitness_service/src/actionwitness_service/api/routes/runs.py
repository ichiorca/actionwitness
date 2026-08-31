"""Run lifecycle, verification, and report routes.

Endpoints (spec v1.9 §15.3, tier T1) — NOT implemented yet:
    POST /runs
    GET  /runs/{id}
    GET  /runs/{id}/events
    POST /runs/{id}/verify
    GET  /runs/{id}/report
"""

from fastapi import APIRouter

router = APIRouter()
