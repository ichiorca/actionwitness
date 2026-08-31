"""FastAPI application factory — bootstrap plumbing only.

Real routers, workspace-cookie middleware (FR-005/006), rate limiting and
resource caps (§7.1 Tier 1), and startup seeding/cleanup (§29.1) are added with
the Tier 1 vertical slice. Only /healthz exists now so deploys can be smoke-tested.
"""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="ActionWitness", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # spec §29.1
        return {"status": "ok"}

    # TODO(T1): include routers from actionwitness_service.api.routes (§15.1–15.4)
    # TODO(T1): anonymous workspace cookie + authorization (FR-005/FR-006)
    # TODO(T1): per-IP rate limiting, hard resource limits, stale-workspace cleanup (§7.1, §29.1)
    # TODO(T1): mount compiled frontend assets; /demo composition per §29.1
    return app
