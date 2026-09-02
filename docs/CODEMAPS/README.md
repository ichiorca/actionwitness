# CODEMAPS — where things are

Read the one map for the area you are about to touch. Each is a table of
`file → what it owns → what will bite you`, sized so reading it costs less than
opening three files to find out the same thing.

**This is "where". [`../ARCHITECTURE.md`](../ARCHITECTURE.md) is "why".** If you
need to know what a module does, read here. If you need to know why the system is
partitioned this way, or what invariant a change might break, read that.

## Routing

| Working on… | Read |
|---|---|
| Contracts, evaluation, classifications, hashing, replay semantics | [`core.md`](core.md) |
| HTTP endpoints, error envelopes, middleware, SSE, CSP | [`service-api.md`](service-api.md) |
| Orchestration, consent, evidence, audits, benchmarks, evals | [`service-application.md`](service-application.md) |
| SQLite, migrations, locks, config, request logging, CLI | [`platform.md`](platform.md) |
| React workspace, WebMCP tools, polling, confirmation UI | [`frontend.md`](frontend.md) |
| Target adapters, evaluator import, the demo storefront | [`integrations.md`](integrations.md) |
| Which lane a test belongs in, what the gates enforce | [`tests.md`](tests.md) |

## Conventions in these maps

- **Lines** is a size hint, not a metric. It tells you whether opening the file
  is cheap. Anything over ~600 lines is called out as such.
- **Watch for** is the column that earns its keep: an invariant, a trap, or a
  rule that is enforced elsewhere and will fail your build if you break it here.
- A file with no surprises gets one line and no fuss. Absence from a map means
  "nothing you could not guess from the name", not "unimportant".

## Keeping this true

`tests/architecture/test_codemaps.py` fails the build when a map names a path
that does not exist, when a map is missing from the routing table above, or when
a map file exists that nothing routes to. That catches the common decay — a file
renamed, a map orphaned — but it cannot check whether a *description* is still
accurate.

So: if you move a module between layers, split one, or change what it owns,
update its row in the same change. A map that lies is worse than no map, because
the whole point is that a reader trusts it instead of looking.
