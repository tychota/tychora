"""Discardable, workspace-owned JSON syntax facts; never canonical evidence."""

from __future__ import annotations

import json
import os
import stat
from base64 import urlsafe_b64encode
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import cast
from uuid import uuid4

from tychora.adapters.code.capture import parse_cache_key
from tychora.adapters.code.python import PythonAdapter
from tychora.adapters.code.typescript import Dialect, TypeScriptAdapter
from tychora.adapters.files.cas import LocalArtifacts
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.ports.language import SyntaxResult

MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024 * 1024


def file_adapter(relative_path: str):
    suffix = Path(relative_path).suffix.lower()
    if suffix in {".py", ".pyi"}:
        return PythonAdapter()
    dialect = {
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".jsx": "jsx",
    }.get(suffix)
    return TypeScriptAdapter(cast(Dialect, dialect)) if dialect is not None else None


def parse_identity(relative_path: str, options: FrozenJsonMap):
    adapter = file_adapter(relative_path)
    if adapter is None:
        return "unknown", "unsupported", digest("unsupported-adapter-v1")
    caps = adapter.capabilities()
    return (
        caps.language,
        adapter._grammar_name(),
        digest("language-parse-v1", caps.adapter_fingerprint, options),
    )


class SyntaxCache:
    """Validated neutral syntax; paths/revisions are rebound by normalization.

    Atomic JSON entries use the CAS adapter's descriptor-relative directory walk.
    Missing, corrupt, oversized or unsafe entries are misses. No serialized
    executable objects or original byte copies are retained. Backup needs none of
    these facts: the normalizer persists the complete canonical extraction.
    """

    def __init__(self, root: Path):
        try:
            self.files = LocalArtifacts(root)
        except (OSError, TychoraError):
            self.files = None

    @staticmethod
    def key(blob_sha: str, identity, options: FrozenJsonMap) -> str:
        language, grammar, fingerprint = identity
        return parse_cache_key(
            blob_sha,
            grammar,
            {
                "schema": "syntax-cache@1",
                "language": language,
                "adapter": fingerprint,
                "options": options,
                "tree_sitter": version("tree-sitter"),
            },
        )

    def get(
        self, payload: bytes, identity, options: FrozenJsonMap
    ) -> SyntaxResult | None:
        if self.files is None:
            return None
        key = self.key(sha256(payload).hexdigest(), identity, options)
        try:
            with self.files._directory() as directory:
                fd = os.open(
                    key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENTRY_BYTES:
                        return None
                    encoded = stream.read(MAX_ENTRY_BYTES + 1)
            root = json.loads(encoded)
            data = root["syntax"]
            if (
                root["key"] != key
                or root["checksum"] != digest("syntax-cache-payload-v1", data)
                or canonical_bytes(root) != encoded
            ):
                return None
            syntax = SyntaxResult.from_mapping(
                {**data, "file_bytes": urlsafe_b64encode(payload).decode("ascii")}
            )
            self._validate(syntax, payload, identity, options)
            return syntax
        except (OSError, ValueError, TypeError, KeyError, LookupError, TychoraError):
            return None

    @staticmethod
    def _validate(syntax, payload, identity, options):
        if (
            (syntax.language, syntax.grammar, syntax.adapter_fingerprint) != identity
            or syntax.options != options
            or syntax.file_bytes != payload
            or payload.decode(syntax.encoding) != syntax.text
        ):
            raise ValueError("Cached syntax identity differs")

    def put(self, syntax: SyntaxResult, identity, options: FrozenJsonMap) -> None:
        if self.files is None:
            return
        self._validate(syntax, syntax.file_bytes, identity, options)
        key = self.key(sha256(syntax.file_bytes).hexdigest(), identity, options)
        data = syntax.model_dump(mode="json", exclude={"file_bytes"})
        encoded = canonical_bytes(
            {
                "key": key,
                "syntax": data,
                "checksum": digest("syntax-cache-payload-v1", data),
            }
        )
        if len(encoded) > MAX_ENTRY_BYTES:
            return
        staging = ".staging-" + uuid4().hex
        try:
            with self.files._directory() as directory:
                try:
                    fd = os.open(
                        staging,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory,
                    )
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(staging, key, src_dir_fd=directory, dst_dir_fd=directory)
                    entries = []
                    for name in os.listdir(directory):
                        if len(name) != 64 or any(
                            c not in "0123456789abcdef" for c in name
                        ):
                            continue
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode):
                            entries.append((info.st_mtime_ns, name, info.st_size))
                    total = sum(item[2] for item in entries)
                    for _, name, size in sorted(entries):
                        if total <= MAX_CACHE_BYTES:
                            break
                        os.unlink(name, dir_fd=directory)
                        total -= size
                finally:
                    try:
                        os.unlink(staging, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        except (OSError, TychoraError):
            # The cache is optional; canonical retention still owns correctness.
            return
