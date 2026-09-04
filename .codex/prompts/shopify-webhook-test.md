<!-- Use when a Shopify HTTPS webhook handler needs local validation, HMAC failures or duplicate deliveries are suspected, retry behavior is unclear, or development-store and production configuration may be mixed. -->

# Shopify webhook local test

Exercise the Shopify webhook scope named by `$ARGUMENTS`. Treat the argument as an optional topic, fixture path, handler path, or environment label. Default to every implemented Shopify HTTPS webhook handler and the authorized development-store environment.

1. Re-establish the repository baseline before testing. Read `pyproject.toml`, `apps/actionwitness_service/pyproject.toml`, `integrations/shopify/pyproject.toml`, `apps/actionwitness_service/src/actionwitness_service/api/app.py`, `apps/actionwitness_service/src/actionwitness_service/api/routes/shopify.py`, `.env.example`, `render.yaml`, `shopify_bridge/README.md`, and `tests/README.md`. Search manifests, `uv.lock`, imports, routes, configuration, and tests with `rg -n -i 'shopify|webhook|x-shopify|hmac|client.secret|idempot|environment|production' .`.

2. Verify the current implementation rather than assuming one exists. The repository baseline is scaffold-only: no Shopify SDK or HTTP client is declared, the Shopify router is empty and not mounted by `create_app`, `shopify_bridge` has no package manifest, no `shopify.app*.toml` is checked in, and `tests/shopify/` is not implemented. Reconfirm all of these facts. Do not introduce or assume a Shopify SDK, Admin API transport, OAuth flow, webhook subscription, endpoint, secret variable, database table, queue, or environment selector that the code does not declare.

3. Prove route reachability through the application factory. After `uv sync`, run:

   `uv run python -c "from actionwitness_service.api.app import create_app; app=create_app(); print('\n'.join(sorted(f'{method} {route.path}' for route in app.routes for method in getattr(route, 'methods', set()))))"`

   Discover the real webhook path and router prefix from the output and source. Do not invent `/webhooks` or infer reachability merely because `routes/shopify.py` exists.

4. If no mounted webhook handler exists, stop the executable webhook phase and report `NOT RUNNABLE — SCAFFOLD ONLY`. List the missing route, secret configuration, durable delivery store, handler tests, and Shopify app configuration. Continue only with the static development/production separation audit below. Do not fabricate a passing request or add webhook behavior unless `$ARGUMENTS` explicitly requests implementation.

5. If a handler exists, identify its exact route, supported topics, client-secret environment variable, accepted shop identity, persistence boundary, queue/outbox behavior, and response contract from code. Keep vendor DTOs and webhook behavior inside `integrations/shopify` or the service boundary; do not leak Shopify types into `actionwitness_core`.

6. Run the narrowest existing webhook test first with `uv run pytest <discovered-test-file> -q`. If `tests/shopify/` exists, also run `uv run pytest tests/shopify -q`. Do not claim a scenario passed unless the assertion observes durable state or a downstream-effect count, not merely a successful HTTP response.

7. Exercise HMAC verification through `create_app()` using `httpx.AsyncClient` with `httpx.ASGITransport`. Use an existing fixture when available; otherwise use a one-shot `uv run python` probe without adding repository files. Use a synthetic process-local secret under the exact configuration name found in code; never write it to `.env`, source, logs, shell history, or reports. Read the fixture as exact bytes and calculate `base64(HMAC-SHA256(secret, raw_body))`. Send those same bytes without parsing or re-serializing them.

8. Verify this signature matrix:

   - A valid signature and complete required headers reach durable acceptance.
   - A missing signature receives the handler's stable 4xx response and creates no delivery record, queued work, or side effect.
   - An invalid or malformed Base64 signature receives a stable 4xx rather than a 500.
   - A one-byte body mutation after signing fails.
   - Semantically equivalent JSON with different whitespace or key order fails when signed bytes differ.
   - Untrusted topic, shop, IDs, and payload fields are not acted on before signature verification.
   - Verification remains enabled in every environment. Local tests inject a known secret; no development bypass flag is acceptable.
   - If secret rotation is implemented, both active secrets validate during the bounded transition, the retired secret fails afterward, and secrets never appear in diagnostics.

9. Send realistic case-insensitive headers discovered from the handler, including `X-Shopify-Hmac-SHA256`, `X-Shopify-Topic`, `X-Shopify-Shop-Domain`, `X-Shopify-API-Version`, `X-Shopify-Webhook-Id`, and `X-Shopify-Event-Id`. Do not hardwire an API version; read it from the checked-in app configuration or the handler's supported fixture. Treat `X-Shopify-Webhook-Id` as the individual delivery key and `X-Shopify-Event-Id` only as correlation for deliveries caused by the same merchant action.

10. Verify idempotency and concurrency:

    - Send the same valid body twice with the same webhook ID. Both deliveries may receive success responses, but durable acceptance, queued work, and downstream effects must occur exactly once.
    - Send different webhook IDs with the same event ID. Confirm they remain distinct deliveries and are only correlated.
    - Submit the same webhook ID concurrently and prove a database uniqueness boundary or transaction prevents double acceptance.
    - If the same webhook ID is reused with conflicting signed bytes, require an explicit conflict or security signal instead of silently applying a second effect.
    - Recreate the app or repository against the same test database and resend the delivery to prove deduplication survives process restart.
    - Inject a failure between durable acceptance and processing when the architecture supports it. Confirm acknowledgment occurs only after the delivery record and queue/outbox item commit atomically, and confirm resumed processing remains idempotent.

11. Check delivery behavior. Measure that the handler acknowledges well within Shopify's five-second total request timeout and moves slow work off the request path only after durable acceptance. Do not emulate Shopify retries by assuming the CLI trigger performs them; send explicit repeated requests locally. Record that Shopify can retry failed HTTPS deliveries, while the CLI sample trigger itself is not retried and always uses the same sample payload.

12. Audit development versus production separation. Use Shopify's term `development store`, not an assumed generic sandbox. This project permits only one explicitly authorized development store and forbids production-store mutation, checkout, orders, payments, and customer credentials.

    - Confirm the configured store, webhook destination, client secret, persistence, and observability sinks cannot fall through from development to production.
    - Confirm a mode flag cannot disable HMAC verification or select production credentials.
    - Confirm `.env.example` and `render.yaml` currently contain only cart-proof configuration and no webhook client secret or verified environment selector; report this as a readiness gap if unchanged.
    - For a production-labelled `$ARGUMENTS`, run only offline configuration and fixture checks. Do not trigger a production endpoint, create subscriptions, deploy Shopify configuration, or mutate a production store.

13. Optionally perform a Shopify-generated development delivery only when a mounted handler, an authorized development app/store, a checked-in Shopify app configuration, Shopify CLI, and a non-production topic are all verified. Start the real FastAPI factory with:

    `uv run uvicorn actionwitness_service.api.app:create_app --factory --host 127.0.0.1 --port 8000`

    In another shell, run the current CLI shape using values read from configuration:

    `shopify app webhook trigger --api-version=<configured-version> --address=http://localhost:8000/<discovered-route> --topic=<configured-topic>`

    Do not place the client secret directly in the command line. If the repository still lacks `shopify.app*.toml`, skip this step and report the missing configuration. Treat the CLI trigger as sample-delivery coverage only; it does not validate a subscription end to end. Perform a real related action only on the authorized development store and only when explicitly requested.

14. Run the repository checks after the focused exercise:

    - `uv run pytest -q`
    - `uv run pytest tests/architecture -q`
    - If user-facing webhook status or React code changed: run `npm install`, `npm run test`, and `npm run build` from `apps/actionwitness_service/frontend`.

    Do not invent lint or type-check commands. No Python linter, frontend lint script, or frontend type-check script is currently declared. Do not use `npm ci` because no frontend lockfile is checked in. Do not claim the scaffold Dockerfile is a verified production build.

15. Report a concise result containing: implementation status; tested route, topic, fixture, and environment; signature matrix; idempotency/concurrency evidence; development/production separation findings; exact commands and pass/fail counts; skipped live checks with reasons; and remaining gaps. Redact secrets, cookies, customer data, cart tokens, query strings, and raw webhook payloads. Include only payload hashes or bounded synthetic identifiers.

Apply the current Shopify rules documented in [Verify webhook deliveries](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries), [Webhook delivery structure](https://shopify.dev/docs/apps/build/webhooks/delivery-structure), [Manage webhook subscriptions](https://shopify.dev/docs/apps/build/webhooks/subscribe), [Shopify CLI webhook trigger](https://shopify.dev/docs/api/shopify-cli/app/app-webhook-trigger), [App configuration](https://shopify.dev/docs/apps/build/cli-for-apps/app-configuration), [Development stores](https://shopify.dev/docs/apps/build/stores/development-stores), and [Credential management](https://shopify.dev/docs/apps/build/authentication-authorization/manage-credentials).
