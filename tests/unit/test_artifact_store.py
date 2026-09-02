"""Artifact bytes on disk: content-addressed names and all-or-nothing writes.

Two properties, and both exist because the failure they prevent is *worse* than
the artifact being missing. ActionWitness's whole claim is that stored evidence
can be re-verified against its recorded hash, so a stored file that disagrees
with a committed row is not a lost artifact — it is an artifact that reads as
tampered-with, from the one component whose job is to detect tampering.

* **Names carry the digest.** The path used to be a constant per `(workspace,
  run, type)`. A benchmark suite accepts repeated imports while it is `draft`
  (`BenchmarkService.record_import` never leaves that status), so a second
  import overwrote the first file while the first `artifacts` row and every
  trial referencing it stayed live. The tests below drive exactly that sequence.
* **Writes are atomic.** `Path.write_bytes` truncates the destination first, so
  a crash mid-write leaves a committed row pointing at a partial file. The
  interruption is injected at the rename rather than simulated with a mock
  filesystem, so what is asserted is what the operating system actually left
  behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from actionwitness_service.application.artifacts import ArtifactStore

pytestmark = [pytest.mark.unit]

WORKSPACE = "ws_one"
RUN = "run_one"


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def store(artifact_root: Path) -> ArtifactStore:
    return ArtifactStore(artifact_root)


def _files(root: Path) -> list[Path]:
    """Every real file under the artifact root, temporaries included."""
    return sorted(path for path in root.rglob("*") if path.is_file())


def _written(store: ArtifactStore, document: dict[str, object]) -> object:
    return store.write(
        WORKSPACE, RUN, document, artifact_type="evaluator_report", schema_version="1.0"
    )


# --- content-addressed names -------------------------------------------------


def test_two_documents_of_one_type_do_not_overwrite_each_other(store: ArtifactStore) -> None:
    """The reachable case: a second import into a still-`draft` benchmark suite.

    Both `artifacts` rows stay live and both are referenced by trials, so if the
    second write lands on the first one's path, the first row's `content_hash`
    describes bytes that are gone.
    """
    # Arrange / Act
    first = _written(store, {"trials": ["a"]})
    second = _written(store, {"trials": ["b"]})

    # Assert
    assert first.relative_path != second.relative_path
    assert json.loads(store.read_text(first.relative_path)) == {"trials": ["a"]}
    assert json.loads(store.read_text(second.relative_path)) == {"trials": ["b"]}


def test_each_stored_file_still_hashes_to_the_hash_its_row_recorded(
    store: ArtifactStore,
) -> None:
    """The property the path format exists to protect, stated directly."""
    # Arrange
    from actionwitness_core.security.canonical import document_content_hash

    # Act
    first = _written(store, {"trials": ["a"]})
    _written(store, {"trials": ["b"]})

    # Assert — the *first* artifact, after the second one was written.
    stored = json.loads(store.read_text(first.relative_path))
    assert document_content_hash(stored) == first.content_hash


def test_rewriting_an_identical_document_is_idempotent(
    store: ArtifactStore, artifact_root: Path
) -> None:
    """Content addressing must not turn a retry into a second file.

    A retried write carries the same bytes by definition, so it belongs at the
    same path — otherwise every replay would litter the volume with copies.
    """
    # Arrange / Act
    first = _written(store, {"trials": ["a"]})
    again = _written(store, {"trials": ["a"]})

    # Assert
    assert first.relative_path == again.relative_path
    assert first.content_hash == again.content_hash
    assert len(_files(artifact_root)) == 1


# --- all-or-nothing writes ---------------------------------------------------


def test_an_interrupted_write_leaves_no_file_to_mistake_for_evidence(
    store: ArtifactStore, artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash during the rename must leave the destination absent, not partial.

    `os.replace` is the last step, so failing it is the closest reachable stand-in
    for losing power mid-write. What matters is not that the write failed — it is
    that nothing was left at the destination for a reader to take as evidence.
    """
    # Arrange
    import actionwitness_service.application.artifacts as module

    def refuse(source: object, destination: object) -> None:
        raise OSError("the volume went away")

    monkeypatch.setattr(module.os, "replace", refuse)

    # Act
    with pytest.raises(OSError):
        _written(store, {"trials": ["a"]})

    # Assert — no destination file, and no temporary left behind either.
    assert _files(artifact_root) == []


def test_a_completed_write_leaves_no_temporary_behind(
    store: ArtifactStore, artifact_root: Path
) -> None:
    """The ordinary path cleans up after itself.

    Asserted because the temporary is created with `delete=False`: nothing in
    the language removes it, so only the rename does, and a refactor that wrote
    the temporary somewhere else would quietly start accumulating `.part` files
    on the evidence volume.
    """
    # Arrange / Act
    written = _written(store, {"trials": ["a"]})

    # Assert
    assert [path.name for path in _files(artifact_root)] == [Path(written.relative_path).name]


def test_an_overwrite_of_the_same_path_is_never_seen_half_written(
    store: ArtifactStore, artifact_root: Path
) -> None:
    """Replacing an existing file must not truncate it first.

    The same document written twice targets a path that already exists. If the
    implementation opened that path directly, there would be an instant where a
    committed row pointed at zero bytes; the rename has no such instant.
    """
    # Arrange
    first = _written(store, {"trials": ["a"]})

    # Act — a second, identical write over the live file.
    again = _written(store, {"trials": ["a"]})

    # Assert
    assert again.relative_path == first.relative_path
    assert (artifact_root / first.relative_path).read_bytes()
    assert json.loads(store.read_text(again.relative_path)) == {"trials": ["a"]}
