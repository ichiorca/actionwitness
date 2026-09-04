# Build the app from specs (autonomous chain)

Run `cavesson spec-kit implement --continuous --run-tests` with your shell tool. With no `--spec` it auto-starts at the lowest-numbered `specs/NNN-*` and walks the chain; pass `--spec=<id>` only to start mid-chain. Per spec it builds, runs `metadata.testCommand`, runs review + completeness, auto-commits, tags `spec-NNN-complete`, and advances — stopping when specs run out or a gate fails.

STREAMING REALITY: a coding-agent shell tool will usually AUTO-BACKGROUND a run this long no matter how you launch it (a ~10-min foreground cap in the agent runtime — NOT a cavesson choice). So:
- BEST for a live stream: tell the operator to run `cavesson spec-kit implement --continuous --run-tests` directly in a TERMINAL (their IDE's integrated terminal, or the session's `!` prefix). The cavesson streams `--output-format stream-json` + per-spec phase lines straight to them with no cap. Offer this first.
- If you run it in-session and the runtime backgrounds it, TAIL the output file and relay new lines CONTINUOUSLY (a short Monitor/until loop, every few seconds) — NOT only at spec milestones. The operator wants to watch it; keep relaying until the chain ends.
Either way it auto-commits + tags `spec-NNN-complete` per spec and stops on the first gate failure; resume a cut-off run with `--mode resume`.

CONFIRM with the operator first that (a) the spec set exists and has been reviewed (`/cavesson-specify`), and (b) the cost cap is acceptable. After a stop, fix the issue and resume with `--mode resume`. For a single spec interactively (you do the work, Stop hook gates), use `/spec-kit-implement <id>` instead.
