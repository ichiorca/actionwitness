# Deployment

ActionWitness ships as one Docker image and one public origin while preserving two
processes and two Python environments inside the container. The assurance service
never imports the demo target; their only connection is the target's versioned
HTTP API on loopback.

## Local image

```bash
docker build -t actionwitness .
docker run --rm -p 8000:8000 \
  -e HARNESS_PUBLIC_ORIGIN=http://localhost:8000 \
  actionwitness
```

Routes:

| Path | Purpose |
|---|---|
| `/` | React workspace |
| `/api/v1` | Harness API |
| `/demo` | Buggy Store storefront |
| `/demo/api/v1` | Proxied target API |
| `/healthz` | Liveness and readiness detail |

## Required configuration

`HARNESS_PUBLIC_ORIGIN` must equal the exact deployed origin. It drives the origin
allowlist and secure-cookie behavior. The complete environment model is defined in
`apps/actionwitness_service/src/actionwitness_service/config.py`; safe examples are
in `.env.example`.

Never place credentials in the image, frontend bundle, repository, evidence,
fixtures, logs, or health response.

## Health behavior

`/healthz` reports the resolved public origin, static assets, schema version,
database reachability, and origin-policy state. It returns `503` with an explicit
degraded status when the database cannot be read or a production deployment lacks
a valid public origin.

The probe reads real application state rather than returning a startup constant.
Use it as the provider health check so a broken revision does not replace the last
healthy deployment.

## Process lifecycle

`scripts/docker-entrypoint.sh` remains PID 1, forwards termination signals to the
service and store, and reaps both children. The store is intentionally not
auto-restarted inside the container: if it becomes unavailable, target operations
surface `TARGET_UNAVAILABLE` instead of silently converting an unobservable run
into a pass.

The service uses one worker. Adding workers changes the SQLite concurrency model
and is not a scaling toggle.

## Render demo

The public demo is <https://actionwitness.onrender.com>. The free plan can sleep
after fifteen idle minutes, and the cold start that follows takes roughly thirty
seconds. `.github/workflows/keep-warm.yml` pings `/healthz` every five minutes as
a mitigation, not a guarantee, so still warm `/healthz` before a live presentation
and keep the recorded video available. A production deployment that must never
cold-start needs an appropriate paid or reserved runtime.

Demo data is ephemeral. A redeploy starts from deterministic seeded state.

## Release verification

Before promoting a deployment:

1. Run every command in [submission evidence](SUBMISSION_EVIDENCE.md).
2. Build the Docker image used by the deployment.
3. Verify `/healthz`, `/`, `/demo`, and one complete false-success journey.
4. Record the source commit, CI run, deployment revision or image digest, and demo
   video in the release notes.
5. Follow [the release checklist](release-checklist.md); do not waive a failed gate.
