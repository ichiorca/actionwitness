# Recompile the native adapter surface

Run `cavesson apply` with your shell tool to recompile the project's manifest (cavesson.cue or cavesson.json — whichever exists) into the enabled adapters' native files (skills, commands, settings.json, hooks, AGENTS.md). This CHANGES on-disk config — confirm with the operator first if it wasn't they who asked. Report which adapters recompiled and how many files changed; note that hook/settings changes take effect in the NEXT session.
