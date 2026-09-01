"""Auditable, hash-guarded file change sets for local agent runs.

The journal deliberately lives outside the editable workspace.  It records the
state immediately before and after a mutation, so a later desktop client can
render a stable review surface and offer a guarded rollback.  A rollback never
overwrites a file that changed after the recorded mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from seecoder.types import ToolCall, ToolResult


_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_FILE_MUTATIONS = frozenset(
    {"write_file", "apply_patch", "delete_file", "copy_file", "move_file"}
)
_DIRECTORY_MUTATIONS = frozenset({"create_directory", "rename_directory"})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_arguments(call: ToolCall) -> tuple[str, ...]:
    """Return candidate workspace-relative paths from a validated tool call."""

    try:
        arguments = json.loads(call.arguments)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(arguments, dict):
        return ()
    names = {
        "write_file": ("path",),
        "apply_patch": ("path",),
        "delete_file": ("path",),
        "create_directory": ("path",),
        "rename_directory": ("path",),
        "copy_file": ("source", "destination"),
        "move_file": ("source", "destination"),
    }.get(call.name, ())
    return tuple(value for name in names if isinstance((value := arguments.get(name)), str) and value.strip())


@dataclass(slots=True)
class FileState:
    path: str
    exists: bool
    is_file: bool
    sha256: str | None
    blob: str | None = None
    size_bytes: int = 0


@dataclass(slots=True)
class ChangeSet:
    id: str
    run_id: str
    workspace: str
    created_at: str
    records: list[dict[str, Any]] = field(default_factory=list)
    directory_operations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        return [str(record["path"]) for record in self.records]


class ChangeSetJournal:
    """Collect mutations for one run and persist reversible file baselines."""

    def __init__(self, workspace: Path, storage_dir: Path | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.storage_dir = storage_dir.expanduser().resolve() if storage_dir else None
        if self.storage_dir is not None:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.active: ChangeSet | None = None

    def start(self, run_id: str | None = None) -> ChangeSet:
        self.active = ChangeSet(
            id=str(uuid.uuid4()),
            run_id=run_id or str(uuid.uuid4()),
            workspace=str(self.workspace),
            created_at=datetime.now(UTC).isoformat(),
        )
        return self.active

    def update_workspace(self, workspace: Path) -> None:
        """Follow a permitted workspace-root rename for subsequent mutations."""

        resolved = workspace.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not an existing directory: {workspace}")
        self.workspace = resolved
        if self.active is not None:
            self.active.workspace = str(resolved)
            self._persist()

    def capture_before(self, call: ToolCall) -> dict[str, FileState]:
        if call.name not in _FILE_MUTATIONS | _DIRECTORY_MUTATIONS:
            return {}
        states: dict[str, FileState] = {}
        for raw_path in _path_arguments(call):
            path = self._resolve(raw_path)
            if path is None:
                continue
            relative = path.relative_to(self.workspace).as_posix() or "."
            states[relative] = self._state(relative, path)
        return states

    def record(
        self, call: ToolCall, result: ToolResult, before: dict[str, FileState]
    ) -> dict[str, Any] | None:
        if self.active is None or call.name not in _FILE_MUTATIONS | _DIRECTORY_MUTATIONS:
            return None
        data = result.data if isinstance(result.data, dict) else {}
        paths = self._result_paths(call, data, before)
        if call.name in _DIRECTORY_MUTATIONS:
            operation = {"tool": call.name, "ok": result.ok, "paths": paths}
            if result.error is not None:
                operation["error"] = result.error.as_dict()
            self.active.directory_operations.append(operation)
            self._persist()
            return {"changeset_id": self.active.id, "tool": call.name, "directory": True, **operation}
        records: list[dict[str, Any]] = []
        for relative in paths:
            path = self._resolve(relative)
            if path is None:
                continue
            previous = before.get(relative) or self._state(relative, path)
            current = self._state(relative, path)
            if previous.exists == current.exists and previous.sha256 == current.sha256:
                continue
            record = {
                "path": relative,
                "before_exists": previous.exists,
                "before_hash": previous.sha256,
                "before_blob": previous.blob,
                "before_size_bytes": previous.size_bytes,
                "after_exists": current.exists,
                "after_hash": current.sha256,
                "after_size_bytes": current.size_bytes,
                "tool": call.name,
            }
            self.active.records.append(record)
            records.append(record)
        if not records:
            return None
        self._persist()
        return {
            "changeset_id": self.active.id,
            "tool": call.name,
            "files": [record["path"] for record in records],
            "records": records,
        }

    def rollback(self, changeset_id: str) -> dict[str, Any]:
        """Restore a completed file ChangeSet if every after-hash still matches."""

        try:
            uuid.UUID(str(changeset_id))
        except (ValueError, AttributeError):
            return {"ok": False, "error": "ChangeSet identifier is invalid."}
        changeset = self._load(changeset_id)
        if changeset is None:
            return {"ok": False, "error": "ChangeSet does not exist."}
        if changeset.workspace != str(self.workspace):
            return {"ok": False, "error": "ChangeSet belongs to a different workspace."}
        conflicts: list[str] = []
        for record in changeset.records:
            path = self._resolve(str(record["path"]))
            if path is None or self._hash_for_path(path) != record.get("after_hash"):
                conflicts.append(str(record["path"]))
        if conflicts:
            return {"ok": False, "error": "Files changed after this ChangeSet.", "conflicts": conflicts}
        restored: list[str] = []
        for record in reversed(changeset.records):
            path = self._resolve(str(record["path"]))
            if path is None:
                continue
            if record.get("before_exists"):
                blob = record.get("before_blob")
                if not blob:
                    return {"ok": False, "error": f"Missing rollback snapshot for {record['path']}."}
                source = self._blob_path(blob)
                path.parent.mkdir(parents=True, exist_ok=True)
                data = source.read_bytes()
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                        handle.write(data)
                        temporary = Path(handle.name)
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            else:
                path.unlink(missing_ok=True)
            restored.append(str(record["path"]))
        return {"ok": True, "changeset_id": changeset_id, "restored": restored}

    def _resolve(self, raw_path: str) -> Path | None:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            path = candidate.resolve(strict=False)
        else:
            path = (self.workspace / candidate).resolve(strict=False)
        try:
            path.relative_to(self.workspace)
        except ValueError:
            return None
        return path

    def _state(self, relative: str, path: Path) -> FileState:
        if not path.exists() or not path.is_file():
            return FileState(relative, path.exists(), False, None)
        size = path.stat().st_size
        if size > _MAX_SNAPSHOT_BYTES:
            return FileState(relative, True, True, None, size_bytes=size)
        data = path.read_bytes()
        digest = _sha256(data)
        blob = self._store_blob(digest, data)
        return FileState(relative, True, True, digest, blob=blob, size_bytes=size)

    def _hash_for_path(self, path: Path) -> str | None:
        if not path.exists() or not path.is_file():
            return None
        if path.stat().st_size > _MAX_SNAPSHOT_BYTES:
            return None
        return _sha256(path.read_bytes())

    def _result_paths(self, call: ToolCall, data: dict[str, Any], before: dict[str, FileState]) -> list[str]:
        candidates: list[str] = list(before)
        for key in ("path", "source", "destination", "old_path", "new_path"):
            value = data.get(key)
            if isinstance(value, str) and value and value != ".":
                path = self._resolve(value)
                if path is not None:
                    candidates.append(path.relative_to(self.workspace).as_posix())
        return list(dict.fromkeys(candidates))

    def _store_blob(self, digest: str, data: bytes) -> str | None:
        if self.storage_dir is None:
            return None
        target = self.storage_dir / "blobs" / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}")
            temporary.write_bytes(data)
            os.replace(temporary, target)
        return digest

    def _blob_path(self, digest: str) -> Path:
        if self.storage_dir is None:
            raise ValueError("Rollback requires a persistent ChangeSet storage directory.")
        path = self.storage_dir / "blobs" / digest
        if not path.is_file():
            raise ValueError("Rollback snapshot is missing.")
        return path

    def _persist(self) -> None:
        if self.storage_dir is None or self.active is None:
            return
        destination = self.storage_dir / f"{self.active.id}.json"
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}")
        temporary.write_text(json.dumps(asdict(self.active), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)

    def _load(self, changeset_id: str) -> ChangeSet | None:
        if self.storage_dir is None:
            if self.active is None or self.active.id != changeset_id:
                return None
            return self.active
        path = self.storage_dir / f"{changeset_id}.json"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return ChangeSet(
                id=str(raw["id"]), run_id=str(raw["run_id"]), workspace=str(raw["workspace"]),
                created_at=str(raw["created_at"]), records=list(raw.get("records", [])),
                directory_operations=list(raw.get("directory_operations", [])),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
