"""aiosqlite repository implementations of actionwitness_core.ports.Repository.

Spec §17: WAL, foreign keys, 5000 ms busy timeout, BEGIN IMMEDIATE for serialized
workspace mutations, no transaction held across browser I/O or confirmation waits,
and never a blocking driver call on the event loop. Scaffolding only.
"""
