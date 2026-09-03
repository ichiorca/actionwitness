# GitHub presentation settings

Repository files cannot configure GitHub's About panel or social preview. Use these
prepared values when updating the repository settings after the final presentation
commit is pushed.

## About

**Description**

> Independent verification for WebMCP actions—authoritative state, human consent,
> and replayable regressions.

**Website**

<https://actionwitness.onrender.com>

**Topics**

`webmcp`, `mcp`, `ai-agents`, `agent-evaluation`, `testing`, `observability`,
`fastapi`, `react`, `typescript`, `python`

## Social preview

Upload `docs/assets/social-preview.png`. It is 1280×640 and keeps its title and
main claim readable at repository-card size.

## Release metadata

Create the release only after the presentation changes are committed and CI is
green. Recommended title: `ActionWitness — WebMCP Challenge build`.

The release notes should include:

- immutable source commit;
- passing CI run for that commit;
- deployed origin and deployment revision or image digest;
- stable demo-video URL;
- link to `docs/SUBMISSION_EVIDENCE.md`;
- known deviations, or `none`.

Do not tag or publish a release from a dirty worktree.
