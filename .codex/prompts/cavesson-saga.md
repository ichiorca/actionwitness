# Inspect cavesson sagas

Run `cavesson saga list` with your shell tool to show each declared saga and its state; for detail on one, run `cavesson saga status <name>`. Report progression (current step, completed steps, any compensations or rollbacks). Do not abort or compensate a saga unless the operator explicitly asks.
