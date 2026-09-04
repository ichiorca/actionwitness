# Run spec-kit completeness

HC1-HC5 structural detectors that catch the "looks done, isn't done" failure
patterns: unreachable modules, unused params, stub migrations, AC misalignment,
fixture shortfalls.

## Run it

```
cavesson spec-kit completeness
```

Each detector emits findings independently. Pass `--check=HC1` etc. to limit
scope. Exit code 1 on findings; 2 on config error. Read the human-readable
report and resolve every HIGH finding before marking the spec complete.
