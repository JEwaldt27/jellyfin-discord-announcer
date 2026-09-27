"""Build identity for the announcer.

`__version__` is bumped by hand for releases. The commit and build date come
from Docker build args, so they are only present when the image was built with
them. The fingerprint is computed at runtime from the source that is actually
loaded, which means it is always available and cannot drift from reality.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.6"

# Injected by the Dockerfile from build args. Absent when running from a plain
# checkout, or when the image was built without passing them.
GIT_COMMIT = os.environ.get("GIT_COMMIT", "").strip() or None
BUILD_DATE = os.environ.get("BUILD_DATE", "").strip() or None

STARTED_AT = datetime.now(timezone.utc)

_SOURCE_DIR = Path(__file__).resolve().parent


def source_fingerprint() -> str:
    """A digest of the bot's own source files.

    This exists because a version string can lie: it reports what someone
    remembered to bump, not what is running. The fingerprint is derived from
    the files themselves, so comparing it against the same digest taken on a
    checkout answers "is the container running this code?" directly.

    Line endings are normalised so a CRLF checkout on Windows and the LF copy
    inside the image produce the same digest for identical code.

    This covers every *.py beside this file, so it stays honest only while the
    Dockerfile's COPY list matches the repo's .py files. Add a module to one
    and not the other and the two digests will disagree for a harmless reason.
    """
    digest = hashlib.sha256()
    for path in sorted(_SOURCE_DIR.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]


if __name__ == "__main__":
    print(f"{__version__} {source_fingerprint()}")
