"""Reading a `.env` file into the mapping the composition root passes on.

This repository has always *documented* a `.env`: `.env.example` lists every
variable, `.gitignore` keeps the real one out of history, and
`scripts/scan_for_secrets.py` refuses a commit that tracks it. Nothing read it.
An operator who put a model credential in `.env` and started the service got a
process whose environment had never heard of it, and a capability bar that said
`disabled` for a module they had just configured — a silent gap between the
documentation and the deployment, which is the failure mode this whole product
is about.

**Parsing is pure and separate from reading.** `parse_env_file` takes text and
returns a mapping, so every malformed-line case is testable without a
filesystem; `read_env_file` is the one function that touches disk. `config.py`
keeps taking an injected mapping and stays unaware that files exist.

**The process environment wins.** A variable set explicitly on the command line
or by a container orchestrator is a deliberate act performed now; a `.env` file
is a default written earlier. Letting the file override the process would make
`FOO=bar uv run ...` silently do nothing, and would be the more surprising of
the two possible precedences by a wide margin.

**Nothing here logs a value.** The whole point of this file is that it carries
secrets. Names are safe to report and are what an operator needs in order to
debug a missing variable; values are never rendered, not even truncated, not
even at debug level.

**Malformed input disables nothing.** A line this cannot parse is skipped and
counted, in keeping with the rule the configuration layer already follows:
construction never raises, and a bad value disables only its own module with a
reason attached. A `.env` with one stray line must not take down a service that
would otherwise start.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

__all__ = ["DEFAULT_ENV_FILE", "ENV_FILE_VARIABLE", "compose_environment", "parse_env_file"]

_logger = logging.getLogger("actionwitness.config")

#: The conventional name, and the one `.env.example` tells an operator to copy.
DEFAULT_ENV_FILE = ".env"

#: Points the loader somewhere else. Read from the *process* environment, never
#: from a file — a `.env` that could redirect the loader to another `.env` is a
#: loop with no natural end.
ENV_FILE_VARIABLE = "HARNESS_ENV_FILE"

#: A generous cap. A `.env` is a short list of variables; anything this large is
#: a file somebody pointed at by mistake, and reading it whole into memory to
#: discover that would be the wrong order of operations.
MAX_ENV_FILE_BYTES = 256 * 1024


def parse_env_file(text: str) -> dict[str, str]:
    """Parse `KEY=value` lines. Unparseable lines are skipped, never fatal.

    Deliberately not a shell parser. It handles what a `.env` actually contains
    — comments, blank lines, an optional `export` prefix, and one layer of
    matching quotes — and nothing else. Command substitution, variable
    interpolation and line continuations are *not* supported, because
    supporting them would mean evaluating the contents of a file that exists to
    hold secrets, and a configuration file should not be a program.
    """
    values: dict[str, str] = {}
    skipped = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _is_variable_name(key):
            skipped += 1
            continue

        values[key] = _unquote(value.strip())

    if skipped:
        # A count, never the lines themselves: an unparseable line in this file
        # is as likely as any other to contain a credential.
        _logger.warning("skipped %d unparseable line(s) in the environment file", skipped)
    return values


def read_env_file(path: Path) -> dict[str, str]:
    """Read and parse one env file. A missing or unreadable file is empty.

    Absence is the common case — most deployments configure the process
    directly — so it is not an error and not a warning. An unreadable file *is*
    worth a warning, because somebody meant it to be read.
    """
    try:
        if not path.is_file():
            return {}
        if path.stat().st_size > MAX_ENV_FILE_BYTES:
            _logger.warning("environment file %s is too large to read; ignoring it", path.name)
            return {}
        text = path.read_text(encoding="utf-8")
    except OSError:
        # The name, not the path: an absolute path in a log line is one more
        # thing that leaks about the host than it needs to.
        _logger.warning("could not read the environment file %s; ignoring it", path.name)
        return {}
    except UnicodeDecodeError:
        _logger.warning("environment file %s is not UTF-8; ignoring it", path.name)
        return {}
    return parse_env_file(text)


def compose_environment(
    process_environ: Mapping[str, str] | None = None, *, root: Path | None = None
) -> Mapping[str, str]:
    """The mapping `ServiceSettings.from_env` should be given.

    The file underneath, the process on top. Returns a plain dict rather than a
    live view, so the settings a deployment resolved cannot change afterwards
    because something else edited `os.environ`.
    """
    environ = os.environ if process_environ is None else process_environ
    path = Path(environ.get(ENV_FILE_VARIABLE) or ((root or Path.cwd()) / DEFAULT_ENV_FILE))

    composed = dict(read_env_file(path))
    if composed:
        # Names only. This line is what turns "the module says disabled and I
        # configured it" into a one-look diagnosis, and it stays safe to paste
        # into a bug report.
        _logger.info(
            "loaded %d variable(s) from %s: %s",
            len(composed),
            path.name,
            ", ".join(sorted(composed)),
        )
    composed.update(environ)
    return composed


def _is_variable_name(candidate: str) -> bool:
    """POSIX-ish: letters, digits and underscore, not starting with a digit."""
    return bool(candidate) and not candidate[0].isdigit() and _is_name_body(candidate)


def _is_name_body(candidate: str) -> bool:
    return all(
        character == "_" or (character.isascii() and character.isalnum()) for character in candidate
    )


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes, and nothing more.

    A quoted value keeps its interior whitespace, which is the reason to quote
    one in the first place. An unquoted value has already been stripped by the
    caller. Escape sequences are left alone: `\\n` in a `.env` is two characters
    in every tool that reads these files, and quietly turning it into a newline
    would corrupt a key that legitimately contains a backslash.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value
