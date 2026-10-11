from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalConfig,
    OperationalError,
    append_ledger,
    assert_private_directory,
    atomic_write_bytes,
    canonical_path,
    clean_environment,
    file_sha256,
    run_bounded,
    safe_executable,
)


class OverlayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay_root: str
    identity: str
    telegram_python: str
    import_origin: str
    canonical_manifest_sha256: str


def _assert_active_pth_isolated(config: OperationalConfig, interpreter_site: Path) -> None:
    """Keep mutable active-overlay state out of the disposable gate interpreter."""

    active_pth = Path(config.pth_path).resolve(strict=False)
    site = interpreter_site.resolve(strict=True)
    if active_pth.parent == site:
        raise OperationalError("ACTIVE_PTH_COLLIDES_WITH_DISPOSABLE_INTERPRETER")


def tree_digest(root: Path) -> str:
    directory = canonical_path(root, code="OVERLAY_ROOT_UNSAFE")
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_installer(path: Path) -> object:
    spec = importlib.util.spec_from_file_location("cus_disposable_overlay_installer", path)
    if spec is None or spec.loader is None:
        raise OperationalError("OVERLAY_INSTALLER_INVALID")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_head(repository: Path, home: Path) -> str:
    result = run_bounded(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=repository,
        env=clean_environment(home),
        timeout_seconds=30,
        maximum_output=4096,
    )
    if result.returncode != 0:
        raise OperationalError("OVERLAY_COMMIT_UNAVAILABLE")
    return result.stdout.decode("ascii", errors="strict").strip()


def _render_overlay_tree(
    config: OperationalConfig,
    *,
    expected_telegram_commit: str,
    destination: Path,
) -> tuple[str, str]:
    root = assert_private_directory(Path(config.root), create=False, code="RUNNER_ROOT_UNSAFE")
    cus = canonical_path(Path(config.cus_repository), code="CUS_REPOSITORY_UNSAFE")
    telegram = canonical_path(Path(config.telegram_repository), code="TELEGRAM_REPOSITORY_UNSAFE")
    if _git_head(telegram, root / "overlay-git-home") != expected_telegram_commit:
        raise OperationalError("TELEGRAM_COMMIT_MISMATCH")
    installer_path = cus / "adapters/telegram-ai-agent/install_cus_command_integration.py"
    current_manifest = cus / "adapters/telegram-ai-agent/compatibility/current.json"
    commit_manifest = current_manifest.with_name(f"{expected_telegram_commit}.json")
    if current_manifest.read_bytes() != commit_manifest.read_bytes():
        raise OperationalError("CANONICAL_MANIFEST_MISMATCH")
    manifest_sha = file_sha256(current_manifest, code="CANONICAL_MANIFEST_UNSAFE")
    try:
        manifest = json.loads(current_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationalError("CANONICAL_MANIFEST_INVALID") from exc
    if manifest.get("supported_base_commit") != expected_telegram_commit:
        raise OperationalError("CANONICAL_MANIFEST_STALE")
    targets = manifest.get("target_files")
    if not isinstance(targets, dict) or not targets:
        raise OperationalError("CANONICAL_MANIFEST_INVALID")
    for relative, expected in targets.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise OperationalError("CANONICAL_MANIFEST_INVALID")
        if file_sha256(telegram / relative, code="TELEGRAM_SOURCE_UNSAFE") != expected:
            raise OperationalError("TELEGRAM_SOURCE_HASH_MISMATCH")

    shutil.copytree(telegram / "src/telegram_bot", destination / "src/telegram_bot")
    installer = _load_installer(installer_path)
    patch = getattr(installer, "_patch_overlay", None)
    if not callable(patch):
        raise OperationalError("OVERLAY_INSTALLER_INVALID")
    fixture = root / "overlay-fixture"
    fixture.mkdir(mode=0o700, exist_ok=True)
    for name, content in {
        "miniapp-url.txt": "https://invalid.example/qualification\n",
        "project-bindings.json": '{"bindings":{}}\n',
        "project-settings.json": '{}\n',
        "runtime-specs.json": '{"schema_version":1,"projects":{}}\n',
        "runtime.lock": "",
        "runtime-journal.json": '{}\n',
    }.items():
        atomic_write_bytes(fixture / name, content.encode("utf-8"))
    patch(
        destination,
        fixture / "miniapp-url.txt",
        fixture / "project-bindings.json",
        fixture / "control.sock",
        root / "profiles",
        cus / "adapters/telegram-ai-agent/cus_control_template.py",
        cus / "adapters/telegram-ai-agent/cus_state_template.py",
        fixture / "project-settings.json",
        fixture / "runtime-specs.json",
        fixture / "runtime.lock",
        fixture / "runtime-journal.json",
        Path(config.approved_codex_binary),
        cus / "adapters/telegram-ai-agent/cus_runtime_template.py",
    )
    return tree_digest(destination), manifest_sha


def render_overlay_identity(
    config: OperationalConfig, *, expected_telegram_commit: str
) -> tuple[str, str]:
    root = assert_private_directory(Path(config.root), create=False, code="RUNNER_ROOT_UNSAFE")
    renders = assert_private_directory(
        root / "overlay-renders", create=True, code="OVERLAY_ROOT_UNSAFE"
    )
    with tempfile.TemporaryDirectory(prefix=".render-", dir=renders) as temporary_name:
        staged = Path(temporary_name) / "overlay"
        return _render_overlay_tree(
            config,
            expected_telegram_commit=expected_telegram_commit,
            destination=staged,
        )


def build_overlay(
    config: OperationalConfig,
    *,
    expected_telegram_commit: str,
    replace_existing: bool = False,
) -> OverlayResult:
    root = assert_private_directory(Path(config.root), create=False, code="RUNNER_ROOT_UNSAFE")
    telegram = canonical_path(Path(config.telegram_repository), code="TELEGRAM_REPOSITORY_UNSAFE")
    cus = canonical_path(Path(config.cus_repository), code="CUS_REPOSITORY_UNSAFE")

    overlay_parent = assert_private_directory(
        Path(config.overlay_root).parent, create=True, code="OVERLAY_ROOT_UNSAFE"
    )
    final_overlay = Path(config.overlay_root)
    with tempfile.TemporaryDirectory(prefix=".overlay-", dir=overlay_parent) as temporary_name:
        staged = Path(temporary_name) / "overlay"
        identity, manifest_sha = _render_overlay_tree(
            config,
            expected_telegram_commit=expected_telegram_commit,
            destination=staged,
        )
        if final_overlay.exists():
            if tree_digest(final_overlay) != identity:
                if not replace_existing or config.mode != "qualification":
                    raise OperationalError("OVERLAY_EXISTING_MISMATCH")
                previous = overlay_parent / f".{final_overlay.name}.previous-{os.getpid()}"
                if previous.exists():
                    raise OperationalError("OVERLAY_REPLACEMENT_UNSAFE")
                os.replace(final_overlay, previous)
                try:
                    os.replace(staged, final_overlay)
                except Exception:
                    os.replace(previous, final_overlay)
                    raise
                shutil.rmtree(previous)
        else:
            os.replace(staged, final_overlay)

    interpreter_root = assert_private_directory(
        root / "telegram-interpreter", create=True, code="TELEGRAM_INTERPRETER_UNSAFE"
    )
    interpreter = interpreter_root / "bin/python"
    if not interpreter.exists():
        result = run_bounded(
            ["/usr/bin/python3", "-m", "venv", "--copies", str(interpreter_root)],
            cwd=root,
            env=clean_environment(root),
            timeout_seconds=120,
        )
        append_ledger(
            config,
            runner="overlay",
            operation="create-interpreter",
            argv=["/usr/bin/python3", "-m", "venv", "--copies", str(interpreter_root)],
            cwd=root,
            result=result,
            evidence_class="PROCESS_INTEGRATION",
            result_code="PASS" if result.returncode == 0 else "FAILED",
        )
        if result.returncode != 0:
            raise OperationalError("TELEGRAM_INTERPRETER_FAILED")
    executable = safe_executable(interpreter, code="TELEGRAM_INTERPRETER_UNSAFE", allowed_root=root)
    dependency_sites = [
        path.resolve(strict=True)
        for path in (telegram / ".venv/lib").glob("python*/site-packages")
        if path.is_dir() and not path.is_symlink()
    ]
    if len(dependency_sites) != 1:
        raise OperationalError("TELEGRAM_SITE_PACKAGES_UNAVAILABLE")
    dependency_site = dependency_sites[0]
    q_site_result = run_bounded(
        [str(executable), "-c", "import site; print(site.getsitepackages()[0])"],
        cwd=root,
        env=clean_environment(root / "telegram-q-site-home"),
        timeout_seconds=30,
        maximum_output=4096,
    )
    if q_site_result.returncode != 0:
        raise OperationalError("TELEGRAM_INTERPRETER_FAILED")
    q_site = Path(q_site_result.stdout.decode("utf-8", errors="strict").strip())
    q_site.chmod(0o700)
    _assert_active_pth_isolated(config, q_site)
    pth = q_site / "00-cus-disposable-overlay.pth"
    pth_payload = "\n".join(
        [
            str(final_overlay / "src"),
            str(cus / "backend/ai-account-manager/src"),
            str(dependency_site),
            "",
        ]
    ).encode("utf-8")
    atomic_write_bytes(pth, pth_payload)
    compile_result = run_bounded(
        [str(executable), "-m", "compileall", "-q", str(final_overlay / "src")],
        cwd=root,
        env=clean_environment(root / "telegram-compile-home"),
        timeout_seconds=120,
    )
    append_ledger(
        config,
        runner="overlay",
        operation="compile",
        argv=[str(executable), "-m", "compileall", "-q", str(final_overlay / "src")],
        cwd=root,
        result=compile_result,
        evidence_class="PROCESS_INTEGRATION",
        result_code="PASS" if compile_result.returncode == 0 else "FAILED",
    )
    if compile_result.returncode != 0:
        raise OperationalError("OVERLAY_COMPILE_FAILED")
    import_result = run_bounded(
        [
            str(executable),
            "-c",
            "import telegram_bot.core.services.codex_rollout as m; print(m.__file__)",
        ],
        cwd=root,
        env=clean_environment(root / "telegram-import-home"),
        timeout_seconds=60,
        maximum_output=4096,
    )
    append_ledger(
        config,
        runner="overlay",
        operation="import-origin",
        argv=[str(executable), "-c", "<fixed import-origin probe>"],
        cwd=root,
        result=import_result,
        evidence_class="PROCESS_INTEGRATION",
        result_code="PASS" if import_result.returncode == 0 else "FAILED",
    )
    if import_result.returncode != 0:
        raise OperationalError("OVERLAY_IMPORT_FAILED")
    origin = Path(import_result.stdout.decode("utf-8", errors="strict").strip()).resolve()
    if not origin.is_relative_to(final_overlay.resolve()):
        raise OperationalError("OVERLAY_IMPORT_ORIGIN_MISMATCH")
    return OverlayResult(
        overlay_root=str(final_overlay),
        identity=identity,
        telegram_python=str(executable),
        import_origin=str(origin),
        canonical_manifest_sha256=manifest_sha,
    )
