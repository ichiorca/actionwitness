"""Shared fixture builders for the workspace test suite (spec v1.9 §26).

Determinism is a constitutional requirement, not a testing preference: injected
clocks, identifiers, and randomness are what make evaluation and replay
reproducible. Providing them here — before any product code exists — means M1
onward has no excuse to reach for `datetime.now()` or `uuid4()` inside a code
path that has to replay identically.

Nothing here touches the network, the wall clock, or process state.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"

CANONICALIZATION_VECTORS = FIXTURE_ROOT / "canonicalization" / "rfc8785_vectors.json"

#: A fixed, timezone-aware instant. Chosen once so that recorded fixtures,
#: regression cases, and expected hashes are stable across machines and years.
EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class FrozenClock:
    """An injected clock that only moves when a test moves it.

    Persisted time is timezone-aware UTC (constitution §1), so this refuses to
    hand out a naive instant even in a test.
    """

    def __init__(self, start: datetime = EPOCH) -> None:
        if start.tzinfo is None:
            raise ValueError("a persisted instant must be timezone-aware")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now

    def __call__(self) -> datetime:
        return self.now()


class IdSequence:
    """A deterministic replacement for random identifiers.

    Counters are per prefix, so a test's run IDs stay readable and stable no
    matter what other identifiers it also allocates.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        current = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = current
        return f"{prefix}-{current:04d}"

    def __call__(self, prefix: str) -> str:
        return self.next(prefix)


@pytest.fixture
def epoch() -> datetime:
    """The project's fixed reference instant."""
    return EPOCH


@pytest.fixture
def clock_factory() -> type[FrozenClock]:
    """The clock class itself, for tests that need a non-default start instant."""
    return FrozenClock


@pytest.fixture
def id_sequence_factory() -> type[IdSequence]:
    """The sequence class itself, for tests comparing two independent runs."""
    return IdSequence


@pytest.fixture
def frozen_clock() -> FrozenClock:
    """A clock that never advances on its own. Never use wall-clock time in a test."""
    return FrozenClock()


@pytest.fixture
def id_sequence() -> IdSequence:
    """Deterministic identifiers, so two runs of a test produce the same evidence."""
    return IdSequence()


@pytest.fixture
def reimported_core() -> Iterator[object]:
    """Import `actionwitness_core` from a clean module table, then put it back.

    Several lane tests prove the core is importable with no integration present
    by dropping it from `sys.modules` and importing it again. Two things make
    that harder than it looks.

    First, done naively it leaves the *reloaded* classes installed for the rest
    of the session, so a model built before the purge and one built after it no
    longer share a class - and an `isinstance` check that is correct in isolation
    starts failing only in a full-suite run, ordered by whichever lane ran first.
    So the module table is snapshotted and restored.

    Second, a leak check that merely scans `sys.modules` afterwards is not
    measuring what the import pulled in; it is measuring what any *other* test
    module imported at collection time, since pytest imports the whole suite
    before running any of it. So the caller names the roots it intends to watch,
    those are purged too, and their presence afterwards means this import really
    did reach them.
    """
    import sys

    snapshot = dict(sys.modules)
    purged: list[str] = ["actionwitness_core"]

    def _drop(roots: Iterable[str]) -> None:
        for module in list(sys.modules):
            if any(module == root or module.startswith(f"{root}.") for root in roots):
                del sys.modules[module]

    def _import(name: str = "actionwitness_core", watch: Iterable[str] = ()):
        purged.extend(watch)
        _drop(purged)
        return importlib.import_module(name)

    try:
        yield _import
    finally:
        _drop(purged)
        sys.modules.update(snapshot)


@pytest.fixture(scope="session")
def canonicalization_vectors() -> dict:
    """The RFC 8785 vector corpus (ADR-0004).

    Session-scoped and read-only: the canonicalizer M1 implements is judged
    against exactly this corpus, so no test may mutate it.
    """
    return json.loads(CANONICALIZATION_VECTORS.read_text(encoding="utf-8"))


@pytest.fixture
def fixture_file():
    """Load a JSON fixture by path relative to `tests/fixtures/`."""

    def _load(relative: str) -> object:
        path = FIXTURE_ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(f"no fixture at tests/fixtures/{relative}")
        return json.loads(path.read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def build_settings():
    """Build `ServiceSettings` from an explicit environment mapping.

    Configuration is resolved from an injected mapping rather than `os.environ`,
    which is what lets a test assert one module's absence in isolation.
    """
    from actionwitness_service.config import ServiceSettings

    def _build(environ: Mapping[str, str] | None = None):
        return ServiceSettings.from_env(dict(environ or {}))

    return _build


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Iterator[Path]:
    """An isolated per-test directory standing in for a workspace's own storage.

    The workspace is the isolation boundary (constitution §2); tests must not
    share one, so this is function-scoped by design.
    """
    target = tmp_path / "workspace"
    target.mkdir()
    yield target


@pytest.fixture(autouse=True, scope="session")
def _artifacts_stay_out_of_the_repository(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Redirect the default artifact root away from the working tree.

    `DEFAULT_ARTIFACT_ROOT` is the relative path `artifacts`, which resolves
    against the process working directory — the repository root when the suite
    runs. Every test that seals a run therefore wrote an outcome report into the
    checkout, and a later `git add -A` swept a few hundred of them into a commit.

    Constitutional, not cosmetic: release artifacts carry "no secrets, local
    paths, private fixtures, generated build debris". Evidence written by a test
    is debris, and it is also *evidence-shaped* debris, which is worse — a
    reader cannot tell a committed report from a real one.

    Autouse and session-scoped because the leak belongs to any test that
    composes an app without naming a root, including tests not yet written. The
    tests that do name their own root are unaffected.
    """
    try:
        import actionwitness_service.config as config
    except ImportError:
        # The core-only and store-only lanes install neither the service nor its
        # artifact writer, so there is nothing to redirect. Skipping keeps those
        # environments genuinely minimal instead of pulling the service in to
        # satisfy a fixture.
        yield
        return

    original = config.DEFAULT_ARTIFACT_ROOT
    config.DEFAULT_ARTIFACT_ROOT = str(tmp_path_factory.mktemp("artifact-root"))
    try:
        yield
    finally:
        config.DEFAULT_ARTIFACT_ROOT = original
