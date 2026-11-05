"""The names no file of aisland carries, kept as digests so the tests that look for them never spell them.

aisland is published (PyPI, its public repository), and runs in workspaces that are not the one it is developed in.
Two groups of names stay out of it: the signal domain and its private bridge (:data:`DOMAIN`, out of every file), and
the private projects, platform, company and development workspace (:data:`PRIVATE`, out of what is published). Each is
the SHA-256 of the name in lower case; :func:`words_named` hashes each word of a line and keeps those that match.
"""

from __future__ import annotations

import hashlib
import re
from typing import FrozenSet, List

#: The signal domain and the private bridge to it: a general project illustrates with `app` / `acme` instead.
DOMAIN: FrozenSet[str] = frozenset(
    {
        "48582bd628b7c80064780ba9ecce2d435db042b40bd4335a7cea4b4c254e8178",
        "a40b2caae0f4487c93d519f065db955ff856259a520be50fdb41b24d951b7f21",
    }
)
#: The private project, the platform, the company and the workspace aisland is developed in.
PRIVATE: FrozenSet[str] = frozenset(
    {
        "699949a69f87ff18a2fd2aa61b2ca9f98adce33ce63711b2f5e8f65b2d8e7e58",
        "98f570982214aad40ef01ce3722194cc122915872a4f554e402a6c09c5f709a5",
        "2db76f21528566a3f39cdaa9f9c8794ae3193e62c8262fd1a476d9cc335f45b5",
        "6702d5c8e392b9d286a8d8077f76a26f10fcd1b878d2cf67fb0d72bab6b70b93",
    }
)

#: A word: a run of letters and digits, a camel-case hump split off its neighbours (``AcmeModels`` → acme, models).
_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])|[0-9]+")


def _digest(word: str) -> str:
    return hashlib.sha256(word.lower().encode()).hexdigest()


def words_named(line: str, digests: FrozenSet[str]) -> List[str]:
    """The words of ``line`` whose digest is one of ``digests``, each as the line writes it."""
    words = set(re.findall(r"[A-Za-z0-9]+", line)) | set(_WORD.findall(line))
    return sorted(word for word in words if _digest(word) in digests)
