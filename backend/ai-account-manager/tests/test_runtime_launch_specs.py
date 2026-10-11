from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from ai_account_manager.core.project_settings import project_id_for_path
from ai_account_manager.core.runtime_launch_specs import (
    RuntimeLaunchSpecStoreError,
    empty_runtime_launch_spec_store,
    get_runtime_launch_spec,
    read_runtime_launch_spec_store,
    reconcile_runtime_launch_spec_store,
    remove_runtime_launch_spec,
    upsert_runtime_launch_spec,
)


def _store_path(tmp_path: Path) -> Path:
    return tmp_path / "private" / "runtime_launch_specs_v1.json"


def _record(project: Path, *, suffix: str = "a") -> dict[str, object]:
    canonical = str(project.resolve(strict=False))
    return {
        "project_id": project_id_for_path(canonical),
        "project_path": canonical,
        "provider_id": "codex",
        "account_profile": f"codex_{suffix}",
        "model": "gpt-5-codex",
        "reasoning_effort": "medium",
        "settings_revision": 1,
        "runtime_revision": 1,
        "spec_sha256": hashlib.sha256(f"spec-{suffix}".encode()).hexdigest(),
        "applied_at": "2026-07-26T00:00:00Z",
        "source": "project_settings_v2",
    }


def test_absent_read_and_get_are_non_mutating(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    assert read_runtime_launch_spec_store(path) == empty_runtime_launch_spec_store()
    assert get_runtime_launch_spec(path, "project_0123456789abcdef0123") is None
    assert not path.exists()
    assert not path.parent.exists()


def test_schema_path_string_secret_and_symlink_validation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _store_path(tmp_path)
    bad_record = _record(project)
    bad_record["model"] = "bad\x01model"
    with pytest.raises(RuntimeLaunchSpecStoreError):
        upsert_runtime_launch_spec(path, bad_record, None)

    bad_sha = _record(project)
    bad_sha["spec_sha256"] = "A" * 64
    with pytest.raises(RuntimeLaunchSpecStoreError):
        upsert_runtime_launch_spec(path, bad_sha, None)

    secret = _record(project)
    secret["env"] = {"CODEX_HOME": "/tmp/profile"}
    with pytest.raises(RuntimeLaunchSpecStoreError) as exc_info:
        upsert_runtime_launch_spec(path, secret, None)
    assert exc_info.value.code == "secret_field_rejected"

    inside_repo = Path.cwd() / "tmp" / "runtime_launch_specs_v1.json"
    with pytest.raises(RuntimeLaunchSpecStoreError) as inside_exc:
        read_runtime_launch_spec_store(inside_repo)
    assert inside_exc.value.code == "invalid_store_path"

    path.parent.mkdir(parents=True)
    target = path.parent / "target.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(RuntimeLaunchSpecStoreError) as symlink_exc:
        read_runtime_launch_spec_store(path)
    assert symlink_exc.value.code == "invalid_store_path"


def test_modes_atomic_fsync_create_update_remove_cas_and_sha_stability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _store_path(tmp_path)
    fsync_calls = 0
    real_fsync = os.fsync

    def counted_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(fd)

    monkeypatch.setattr("ai_account_manager.core.runtime_launch_specs.os.fsync", counted_fsync)
    created = upsert_runtime_launch_spec(path, _record(project), None)
    original_file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert created["runtime_revision"] == 1
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert (path.parent / "runtime_launch_specs_v1.json.lock").stat().st_mode & 0o777 == 0o600
    assert fsync_calls >= 2
    store = read_runtime_launch_spec_store(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_file_sha
    assert store["store_revision"] == 2
    assert store["projects"][created["project_id"]]["runtime_revision"] == 1

    stale = _record(project)
    stale["reasoning_effort"] = "high"
    with pytest.raises(RuntimeLaunchSpecStoreError) as conflict:
        upsert_runtime_launch_spec(path, stale, None)
    assert conflict.value.code == "revision_conflict"

    updated = upsert_runtime_launch_spec(path, stale, 1)
    assert updated["runtime_revision"] == 2
    assert read_runtime_launch_spec_store(path)["store_revision"] == 3
    removed = remove_runtime_launch_spec(path, str(updated["project_id"]), 2)
    assert removed is not None
    assert read_runtime_launch_spec_store(path)["projects"] == {}
    assert read_runtime_launch_spec_store(path)["store_revision"] == 4


def test_concurrent_writers_preserve_records_and_project_isolation(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    projects = [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
    for project in projects:
        project.mkdir()

    def write(project: Path, suffix: str) -> None:
        upsert_runtime_launch_spec(path, _record(project, suffix=suffix), None)

    threads = [
        threading.Thread(target=write, args=(project, suffix))
        for project, suffix in zip(projects, ("a", "b", "c"), strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    store = read_runtime_launch_spec_store(path)
    assert set(store["projects"]) == {project_id_for_path(project) for project in projects}
    first_id = project_id_for_path(projects[0])
    assert get_runtime_launch_spec(path, first_id)["account_profile"] == "codex_a"
    remove_runtime_launch_spec(path, first_id, 1)
    remaining = read_runtime_launch_spec_store(path)["projects"]
    assert first_id not in remaining
    assert len(remaining) == 2


def test_invalid_store_is_quarantined(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeLaunchSpecStoreError) as exc_info:
        read_runtime_launch_spec_store(path)
    assert exc_info.value.code == "runtime_launch_spec_store_invalid"
    assert not path.exists()
    assert list(path.parent.glob("runtime_launch_specs_v1.json.*.invalid"))


def test_reconcile_store_is_atomic_monotonic_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_project = tmp_path / "first"
    second_project = tmp_path / "second"
    first_project.mkdir()
    second_project.mkdir()
    path = _store_path(tmp_path)
    first = upsert_runtime_launch_spec(path, _record(first_project, suffix="a"), None)
    second = upsert_runtime_launch_spec(path, _record(second_project, suffix="b"), None)
    incoming_first = _record(first_project, suffix="a")
    incoming_second = _record(second_project, suffix="b")
    incoming_first["spec_sha256"] = hashlib.sha256(b"official-first").hexdigest()
    incoming_second["spec_sha256"] = hashlib.sha256(b"official-second").hexdigest()

    result = reconcile_runtime_launch_spec_store(path, [incoming_second, incoming_first])

    assert result["changed_count"] == 2
    assert [item["project_id"] for item in result["changed"]] == sorted(
        [first["project_id"], second["project_id"]]
    )
    reconciled = read_runtime_launch_spec_store(path)
    assert reconciled["projects"][first["project_id"]]["runtime_revision"] == 2
    assert reconciled["projects"][second["project_id"]]["runtime_revision"] == 2
    stable = path.read_bytes()
    assert reconcile_runtime_launch_spec_store(path, [incoming_first, incoming_second])[
        "changed_count"
    ] == 0
    assert path.read_bytes() == stable

    failed_first = dict(incoming_first)
    failed_first["spec_sha256"] = hashlib.sha256(b"next-contract").hexdigest()
    monkeypatch.setattr(
        "ai_account_manager.core.runtime_launch_specs._write_store_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("forced")),
    )
    with pytest.raises(OSError, match="forced"):
        reconcile_runtime_launch_spec_store(path, [failed_first, incoming_second])
    assert path.read_bytes() == stable
