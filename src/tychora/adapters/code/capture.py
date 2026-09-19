"""Read-only Git inventory for immutable repository capture."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import selectors
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tychora.adapters.files.cas import LocalArtifacts
from tychora.contracts.acquisition import CapturedFile
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, digest, normalize_source_key
from tychora.ports.execution import ExecutionContext

_MAX_GIT_OUTPUT = 128 * 1024 * 1024
_DEFAULT_EXCLUDES = (
    "**/.env",
    "**/.env.*",
    "**/*.key",
    "**/*.pem",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/node_modules/**",
    "**/vendor/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
)
_OID = re.compile(r"^[0-9a-f]+$")


@dataclass(frozen=True, slots=True)
class _HeadFile:
    path: str
    mode: int
    oid: str


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    root: Path
    paths: tuple[str, ...]
    present_paths: tuple[str, ...]
    worktree_state: tuple[tuple[str, tuple[int, ...] | None], ...]
    head_files: tuple[_HeadFile, ...]
    index_state: tuple[tuple[str, str, str, int], ...]
    commit: str | None
    object_format: str


def parse_cache_key(blob_sha: str, grammar: str, options: JsonValue) -> str:
    """Bind a parse result to exact retained bytes and parser semantics."""
    if re.fullmatch(r"[0-9a-f]{64}", blob_sha) is None or not grammar:
        raise ValueError("Parse cache identity requires a SHA-256 and grammar")
    return digest("parse-cache-v1", blob_sha, grammar, options)


class RepositoryCapture:
    """Dependency-bound Git metadata reader; file bytes come from checked descriptors."""

    def __init__(
        self,
        artifacts: LocalArtifacts,
        *,
        git_executable: Path = Path("/usr/bin/git"),
    ) -> None:
        self._artifacts = artifacts
        self._git = git_executable

    def snapshot(
        self,
        root: Path,
        excludes: tuple[str, ...],
        ctx: ExecutionContext,
        *,
        max_files: int,
    ) -> RepositorySnapshot:
        self._check_git(ctx)
        top = self._run(root, ("rev-parse", "--show-toplevel"), ctx)
        try:
            top_path = Path(top.rstrip(b"\n").decode("utf-8", "strict"))
        except UnicodeDecodeError:
            raise TychoraError(
                "INVALID_REQUEST", "Repository root is not valid UTF-8"
            ) from None
        if top_path != root:
            raise TychoraError(
                "INVALID_REQUEST", "Repository source must name its worktree root"
            )
        patterns = tuple(dict.fromkeys((*_DEFAULT_EXCLUDES, *excludes)))
        pathspec = self._pathspec(patterns)
        inventory = self._run(
            root,
            ("ls-files", "-z", "--cached", "--others", "--exclude-standard", *pathspec),
            ctx,
        )
        paths = self._paths(inventory, patterns)
        if len(paths) > max_files:
            raise TychoraError(
                "INVALID_REQUEST", "Repository capture has too many files"
            )
        stage = self._run(root, ("ls-files", "--stage", "-z", *pathspec), ctx)
        index = self._index(stage, patterns)
        commit = self._commit(root, ctx)
        object_format = (
            self._run(root, ("rev-parse", "--show-object-format"), ctx)
            .strip()
            .decode("ascii", "strict")
        )
        if object_format not in {"sha1", "sha256"}:
            raise TychoraError("CAPABILITY_MISSING", "Unsupported Git object format")
        head = ()
        if commit is not None:
            tree = self._run(
                root,
                ("ls-tree", "-r", "-z", "--full-tree", commit),
                ctx,
            )
            head = self._tree(tree, patterns)
        state: list[tuple[str, tuple[int, ...] | None]] = []
        present: list[str] = []
        for name in paths:
            ctx.checkpoint()
            path = root / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                state.append((name, None))
                continue
            if stat.S_ISLNK(info.st_mode):
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Repository capture contains a symbolic link"
                )
            if not stat.S_ISREG(info.st_mode):
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Repository capture contains a nonregular file"
                )
            stamp = (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            state.append((name, stamp))
            present.append(name)
        return RepositorySnapshot(
            root=root,
            paths=paths,
            present_paths=tuple(present),
            worktree_state=tuple(state),
            head_files=head,
            index_state=index,
            commit=commit,
            object_format=object_format,
        )

    def dirty(
        self,
        snapshot: RepositorySnapshot,
        entries: tuple[CapturedFile, ...],
        ctx: ExecutionContext,
    ) -> bool:
        current = {
            entry.relative_path: (
                bool(entry.mode & stat.S_IXUSR),
                self._blob_oid(entry, snapshot.object_format, ctx),
            )
            for entry in entries
        }
        head = {
            item.path: (bool(item.mode & 0o111), item.oid)
            for item in snapshot.head_files
        }
        return current != head

    def _blob_oid(
        self, entry: CapturedFile, object_format: str, ctx: ExecutionContext
    ) -> str:
        algorithm = hashlib.sha1() if object_format == "sha1" else hashlib.sha256()
        algorithm.update(f"blob {entry.artifact_ref.size_bytes}\0".encode("ascii"))
        with self._artifacts.open_verified(entry.artifact_ref) as stream:
            while block := stream.read(1024 * 1024):
                ctx.checkpoint()
                algorithm.update(block)
        ctx.checkpoint()
        return algorithm.hexdigest()

    def _check_git(self, ctx: ExecutionContext) -> None:
        try:
            value = self._run(None, ("--version",), ctx, capability=True)
        except (FileNotFoundError, PermissionError):
            raise TychoraError(
                "CAPABILITY_MISSING", "System Git is unavailable"
            ) from None
        match = re.fullmatch(rb"git version (\d+)\.(\d+)(?:\..*)?\n?", value)
        if match is None or (int(match[1]), int(match[2])) < (2, 30):
            raise TychoraError("CAPABILITY_MISSING", "System Git is unsupported")

    def _commit(self, root: Path, ctx: ExecutionContext) -> str | None:
        value = self._run(
            root,
            ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
            ctx,
            allow_failure=True,
        ).strip()
        if not value:
            return None
        commit = value.decode("ascii", "strict")
        if _OID.fullmatch(commit) is None:
            raise TychoraError("INTEGRITY_ERROR", "Git returned an invalid commit ID")
        return commit

    @staticmethod
    def _pathspec(patterns: tuple[str, ...]) -> tuple[str, ...]:
        return (
            "--",
            ":(top,glob)**",
            *(f":(top,exclude,glob){pattern}" for pattern in patterns),
        )

    @staticmethod
    def _excluded(path: str, patterns: tuple[str, ...]) -> bool:
        return any(
            fnmatch.fnmatchcase(path, pattern)
            or (
                pattern.startswith("**/")
                and fnmatch.fnmatchcase(path, pattern.removeprefix("**/"))
            )
            for pattern in patterns
        )

    def _paths(self, value: bytes, patterns: tuple[str, ...]) -> tuple[str, ...]:
        result = []
        for raw in value.split(b"\0"):
            if not raw:
                continue
            try:
                name = raw.decode("utf-8", "strict")
                normalized = normalize_source_key(name)
            except (UnicodeDecodeError, ValueError):
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Repository contains a path that cannot be represented safely",
                ) from None
            if normalized != name:
                raise TychoraError(
                    "INVALID_REQUEST", "Repository path is not canonical"
                )
            if not self._excluded(name, patterns):
                result.append(name)
        # Unmerged index stages legitimately repeat one worktree path. Stage
        # multiplicity remains in index_state; bytes are captured once.
        return tuple(sorted(set(result)))

    def _index(
        self, value: bytes, patterns: tuple[str, ...]
    ) -> tuple[tuple[str, str, str, int], ...]:
        result = []
        for record in value.split(b"\0"):
            if not record:
                continue
            try:
                prefix, raw_path = record.split(b"\t", 1)
                raw_mode, raw_oid, raw_stage = prefix.split(b" ")
                path = raw_path.decode("utf-8", "strict")
                mode = raw_mode.decode("ascii")
                oid = raw_oid.decode("ascii")
                stage = int(raw_stage)
                normalize_source_key(path)
            except (UnicodeDecodeError, ValueError):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Git index output is invalid"
                ) from None
            if not self._excluded(path, patterns):
                result.append((path, mode, oid, stage))
        return tuple(sorted(result))

    def _tree(self, value: bytes, patterns: tuple[str, ...]) -> tuple[_HeadFile, ...]:
        result = []
        for record in value.split(b"\0"):
            if not record:
                continue
            try:
                prefix, raw_path = record.split(b"\t", 1)
                raw_mode, kind, raw_oid = prefix.split(b" ")
                path = raw_path.decode("utf-8", "strict")
                mode = int(raw_mode, 8)
                oid = raw_oid.decode("ascii")
                normalize_source_key(path)
            except (UnicodeDecodeError, ValueError):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Git tree output is invalid"
                ) from None
            if self._excluded(path, patterns):
                continue
            if kind != b"blob" or _OID.fullmatch(oid) is None:
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Repository tree contains an unsupported entry"
                )
            result.append(_HeadFile(path, mode, oid))
        return tuple(sorted(result, key=lambda item: item.path))

    def _run(
        self,
        root: Path | None,
        arguments: tuple[str, ...],
        ctx: ExecutionContext,
        *,
        allow_failure: bool = False,
        capability: bool = False,
    ) -> bytes:
        ctx.checkpoint()
        argv = [str(self._git)]
        if root is not None:
            argv.extend(
                [
                    "--no-optional-locks",
                    "-C",
                    str(root),
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                ]
            )
        argv.extend(arguments)
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError):
            if capability:
                raise
            raise TychoraError(
                "CAPABILITY_MISSING", "System Git is unavailable"
            ) from None
        assert process.stdout is not None and process.stderr is not None
        selected = selectors.DefaultSelector()
        selected.register(process.stdout, selectors.EVENT_READ, "stdout")
        selected.register(process.stderr, selectors.EVENT_READ, "stderr")
        output = bytearray()
        error = bytearray()
        try:
            while selected.get_map():
                ctx.checkpoint()
                for key, _events in selected.select(0.05):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selected.unregister(key.fileobj)
                        continue
                    target = output if key.data == "stdout" else error
                    target.extend(block)
                    if len(output) + len(error) > _MAX_GIT_OUTPUT:
                        raise TychoraError(
                            "INVALID_REQUEST", "Git output exceeds capture limits"
                        )
            returncode = process.wait()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        finally:
            selected.close()
        if returncode != 0 and not allow_failure:
            code = "CAPABILITY_MISSING" if capability else "INVALID_REQUEST"
            raise TychoraError(code, "Git repository inspection failed")
        return bytes(output)
