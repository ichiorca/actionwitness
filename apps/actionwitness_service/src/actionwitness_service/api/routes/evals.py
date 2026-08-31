"""Built-in regression eval routes.

Endpoints (spec v1.9 §15.4, tier T2) — NOT implemented yet:
    POST /runs/{id}/evals
    GET  /evals
    GET  /evals/{id}
    GET  /evals/{id}/case.json
    POST /evals/{id}/runs
    GET  /evals/{id}/runs/{run_id}
"""

from fastapi import APIRouter

router = APIRouter()
