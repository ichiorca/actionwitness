"""aiosqlite implementations of the `actionwitness_core.ports` repository protocols.

The core publishes `ContractRepository`, `SnapshotRepository`, `EventRepository`,
`FindingRepository`, and `UnitOfWork`; none of them declares an update or delete
method, because §17.1 makes those tables insert-only or append-only.


Spec §17: WAL, foreign keys, 5000 ms busy timeout, BEGIN IMMEDIATE for serialized
workspace mutations, no transaction held across browser I/O or confirmation waits,
and never a blocking driver call on the event loop. Scaffolding only.
"""
