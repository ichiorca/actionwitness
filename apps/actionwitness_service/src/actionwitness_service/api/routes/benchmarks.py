"""External evaluator import and dual-layer benchmark routes.

Endpoints (spec v1.9 §15.6, tier T2) — NOT implemented yet:
    POST /benchmarks
    POST /benchmarks/{id}/imports
    PUT  /benchmarks/{id}/bindings
    POST /benchmarks/{id}/replay
    POST /benchmarks/{id}/finalize
    GET  /benchmarks/{id}
    GET  /benchmarks/{id}/trials/{trial_id}
    GET  /benchmarks/{id}/report
"""

from fastapi import APIRouter

router = APIRouter()
