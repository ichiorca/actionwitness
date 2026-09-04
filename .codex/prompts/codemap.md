# Update codemaps

Generate (or refresh) a token-lean architecture map of THIS project so future
sessions can navigate it without re-scanning the whole tree. You are producing
the map — read the code, then write the summaries. Keep every file small and
optimized for an LLM to load cheaply.

## Step 1 — Scan the structure

1. Identify the project type (monorepo, single app, library, service).
2. Find the source roots (`src/`, `lib/`, `app/`, `internal/`, `packages/`).
3. Map the entry points (main.go, index.ts, app.py, cmd/*, etc.).

## Step 2 — Write the maps

Create or update these under `docs/CODEMAPS/` (fall back to
`.reports/codemaps/` if docs/ is not writable):

| File | Contents |
|------|----------|
| `architecture.md` | High-level component boundaries + data flow |
| `backend.md` | Routes/handlers → service → store call chains |
| `frontend.md` | Page/route tree, component hierarchy, state flow |
| `data.md` | Tables/models, relationships, migrations |
| `dependencies.md` | External services, third-party libs, shared modules |

Omit a file if the project has nothing for it (e.g. no frontend). Prefer
call-flow chains and file paths over code blocks:

```
# Backend

## Routes
POST /api/users -> UserController.create -> UserService.create -> UserRepo.insert
GET  /api/users/:id -> UserController.get -> UserService.findById -> UserRepo.findById

## Key files
src/services/user.ts   (business logic, ~120 lines)
src/repos/user.ts      (data access, ~80 lines)

## Dependencies
- PostgreSQL (primary store)
- Redis (session cache)
```

## Step 3 — Diff-aware updates

If a map already exists, estimate how much it changed. If the change is large
(more than ~30% of the content), show the diff and ask the operator before
overwriting. Otherwise update in place.

## Step 4 — Freshness header

Start each map with a freshness comment:

```
<!-- codemap | generated: 2026-06-04 | files scanned: 142 | ~800 tokens -->
```

## Rules

- Focus on high-level structure, NOT implementation detail.
- Prefer file paths + function signatures over full code.
- Keep each map under ~1000 tokens so loading it is cheap.
- Use ASCII arrows for flow instead of prose.
- Run this after major features or refactors.
