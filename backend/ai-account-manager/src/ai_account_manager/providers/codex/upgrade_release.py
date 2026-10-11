from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ai_account_manager.core.upgrade_manager import (
    CandidateDescriptor,
    ReleaseDiscoveryResult,
    UpgradeManagerError,
)
from ai_account_manager.providers.codex.upgrade_candidate import (
    CandidateProvenance,
    StagedCandidate,
    discover_official_release,
    observe_binary,
    rebind_candidate_provenance,
    stage_candidate,
    verify_candidate,
)
from ai_account_manager.providers.codex.upgrade_mvp import CODEX_GATE_VERSION
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalConfig,
    OperationalError,
    atomic_write_model,
    assert_private_directory,
    clean_environment,
    file_sha256,
    read_private_bytes,
    read_private_model,
    resolve_active_launcher,
    run_bounded,
)
from ai_account_manager.providers.codex.upgrade_overlay import render_overlay_identity
from ai_account_manager.providers.codex.upgrade_retention import (
    CodexUpgradeRetention,
    LazyCodexUpgradeRetention,
)
from ai_account_manager.providers.codex.upgrade_runtime_spec import reconcile_runtime_spec


def _version_tuple(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise OperationalError("RELEASE_VERSION_INVALID") from exc
    if len(parts) != 3:
        raise OperationalError("RELEASE_VERSION_INVALID")
    return parts  # type: ignore[return-value]


def _git(config: OperationalConfig, repository: Path, *args: str) -> str:
    root = assert_private_directory(Path(config.root), create=False, code="RUNNER_ROOT_UNSAFE")
    result = run_bounded(
        ["/usr/bin/git", *args],
        cwd=repository,
        env=clean_environment(root / "release-git-home"),
        timeout_seconds=30,
        maximum_output=64 * 1024,
    )
    if result.returncode != 0:
        raise OperationalError("RELEASE_SOURCE_IDENTITY_UNAVAILABLE")
    return result.stdout.decode("utf-8", errors="strict").strip()


def _reuse_staged_official_candidate(
    *,
    runtime: Path,
    staging_root: Path,
    scratch: Path,
    release_version: str,
    release_asset_name: str,
    release_archive_sha256: str,
    active_version: str,
    cus_commit: str,
    telegram_commit: str,
    overlay_identity: str,
) -> StagedCandidate | None:
    """Reuse an exact immutable release without reserving space for another download.

    A clean production source deployment may legitimately change only the integration
    binding.  The official release bytes remain reusable when their provenance,
    filesystem identity, digest and reported version all still match the current stable
    metadata.  Any missing or different release falls back to the normal guarded
    download path; an invalid existing identity fails closed.
    """

    candidate_path = runtime / "candidate.json"
    provenance_path = runtime / "candidate-provenance.json"
    if not candidate_path.exists() or not provenance_path.exists():
        return None
    candidate = read_private_model(
        candidate_path,
        CandidateDescriptor,
        code="CANDIDATE_DESCRIPTOR_INVALID",
    )
    provenance = read_private_model(
        provenance_path,
        CandidateProvenance,
        code="CANDIDATE_PROVENANCE_INVALID",
    )
    if (
        candidate.binding.provider_id != "codex"
        or candidate.binding.version != release_version
        or provenance.source != "official_release"
        or provenance.qualification_kind != "OFFICIAL_NEWER_RELEASE"
        or provenance.version != release_version
        or provenance.archive_name != release_asset_name
        or provenance.archive_sha256 != release_archive_sha256
        or provenance.binary_sha256 != candidate.binding.digest_sha256
    ):
        return None

    verified = verify_candidate(candidate, provenance_path)
    binary = Path(candidate.binary_path).resolve(strict=True)
    releases = (staging_root / "releases").resolve(strict=True)
    if not binary.is_relative_to(releases):
        raise OperationalError("CANDIDATE_BINARY_UNSAFE")
    _binary, observed_version, observed_digest = observe_binary(binary, scratch)
    if observed_version != release_version or observed_digest != candidate.binding.digest_sha256:
        raise OperationalError("CANDIDATE_IDENTITY_CHANGED")

    rebound = candidate.model_copy(
        update={
            "active_version": active_version,
            "binding": candidate.binding.model_copy(
                update={
                    "cus_commit": cus_commit,
                    "telegram_commit": telegram_commit,
                    "combined_overlay_identity": overlay_identity,
                    "compatibility_gate_version": CODEX_GATE_VERSION,
                }
            ),
        }
    )
    if rebound == candidate:
        return StagedCandidate(descriptor=candidate, provenance=provenance)
    rebound_provenance = rebind_candidate_provenance(rebound, provenance_path)
    atomic_write_model(candidate_path, rebound)
    atomic_write_model(
        runtime / "candidate-provenance-sha256.json",
        {
            "schema_version": 1,
            "candidate_sha256": file_sha256(candidate_path, code="CANDIDATE_RECORD_UNSAFE"),
            "provenance_sha256": file_sha256(provenance_path, code="CANDIDATE_PROVENANCE_UNSAFE"),
        },
    )
    verify_candidate(rebound, provenance_path)
    if verified.binary_sha256 != rebound_provenance.binary_sha256:
        raise OperationalError("CANDIDATE_PROVENANCE_INVALID")
    return StagedCandidate(descriptor=rebound, provenance=rebound_provenance)


class CodexReleaseDiscovery:
    """Codex-specific stable-channel staging behind the neutral discovery protocol."""

    def __init__(
        self,
        config_path: Path,
        manager_runtime_root: Path,
        retention: CodexUpgradeRetention | LazyCodexUpgradeRetention | None = None,
    ) -> None:
        self.config_path = config_path
        self.manager_runtime_root = manager_runtime_root
        self.retention = retention

    def discover(self) -> ReleaseDiscoveryResult:
        try:
            config = read_private_model(
                self.config_path,
                OperationalConfig,
                code="RELEASE_DISCOVERY_CONFIG_INVALID",
            )
            if config.mode != "production" or not config.production_enabled:
                raise OperationalError("RELEASE_DISCOVERY_DISABLED")
            configured_runtime = Path(config.manager_runtime_root).resolve(strict=False)
            expected_runtime = self.manager_runtime_root.resolve(strict=False)
            if configured_runtime != expected_runtime:
                raise OperationalError("RELEASE_DISCOVERY_RUNTIME_MISMATCH")
            root = assert_private_directory(
                Path(config.root), create=False, code="RUNNER_ROOT_UNSAFE"
            )
            scratch = assert_private_directory(
                root / "release-version-home",
                create=True,
                code="RELEASE_DISCOVERY_ROOT_UNSAFE",
            )
            active = resolve_active_launcher(
                Path(config.active_launcher_path), code="ACTIVE_LAUNCHER_UNSAFE"
            )
            try:
                reference = json.loads(
                    read_private_bytes(
                        Path(config.active_reference), code="ACTIVE_REFERENCE_INVALID"
                    )
                )
            except json.JSONDecodeError as exc:
                raise OperationalError("ACTIVE_REFERENCE_INVALID") from exc
            if (
                not isinstance(reference, dict)
                or reference.get("binary_path") != str(active)
                or reference.get("sha256") != file_sha256(active, code="ACTIVE_REFERENCE_INVALID")
            ):
                raise OperationalError("ACTIVE_REFERENCE_INVALID")
            _active, active_version, active_digest = observe_binary(active, scratch)
            discovered_at = datetime.now(UTC).isoformat()
            try:
                release = discover_official_release()
            except OperationalError as exc:
                return ReleaseDiscoveryResult(
                    provider_id="codex",
                    active_version=active_version,
                    active_digest_sha256=active_digest,
                    discovered_at=discovered_at,
                    failure_code=exc.code,
                )
            if _version_tuple(release.version) <= _version_tuple(active_version):
                return ReleaseDiscoveryResult(
                    provider_id="codex",
                    active_version=active_version,
                    active_digest_sha256=active_digest,
                    discovered_at=discovered_at,
                )

            cus = Path(config.cus_repository).resolve(strict=True)
            telegram = Path(config.telegram_repository).resolve(strict=True)
            if cus != Path(config.production_cus_repository).resolve(strict=True):
                raise OperationalError("RELEASE_CUS_SOURCE_MISMATCH")
            if telegram != Path(config.production_telegram_repository).resolve(strict=True):
                raise OperationalError("RELEASE_TELEGRAM_SOURCE_MISMATCH")
            cus_commit = _git(config, cus, "rev-parse", "HEAD")
            telegram_commit = _git(config, telegram, "rev-parse", "HEAD")
            if cus_commit != config.production_cus_commit or _git(
                config, cus, "status", "--porcelain=v1"
            ):
                raise OperationalError("RELEASE_CUS_SOURCE_MISMATCH")
            if telegram_commit != config.production_telegram_commit or _git(
                config, telegram, "status", "--porcelain=v1"
            ):
                raise OperationalError("RELEASE_TELEGRAM_SOURCE_MISMATCH")
            overlay_identity, _manifest_sha = render_overlay_identity(
                config, expected_telegram_commit=telegram_commit
            )
            staging_root = root / "candidate-store"
            staged = _reuse_staged_official_candidate(
                runtime=expected_runtime,
                staging_root=staging_root,
                scratch=scratch,
                release_version=release.version,
                release_asset_name=release.asset_name,
                release_archive_sha256=release.archive_sha256,
                active_version=active_version,
                cus_commit=cus_commit,
                telegram_commit=telegram_commit,
                overlay_identity=overlay_identity,
            )
            if staged is None:
                if self.retention is not None:
                    self.retention.before_candidate_download()
                staged = stage_candidate(
                    staging_root=staging_root,
                    manager_runtime_root=expected_runtime,
                    approved_binary=active,
                    cus_commit=cus_commit,
                    telegram_commit=telegram_commit,
                    overlay_identity=overlay_identity,
                    gate_version=CODEX_GATE_VERSION,
                    release_metadata=release,
                )
            if self.retention is not None:
                self.retention.register_candidate_release(staged.descriptor, staged.provenance)
            # This private deployment spec is candidate-specific. Keeping the
            # active binary in it makes an otherwise valid operator CHECK fail
            # with RUNTIME_SPEC_STALE, so candidate staging and spec staging
            # are one operation.
            reconcile_runtime_spec(
                Path(config.runtime_specs_path),
                staged.descriptor,
                account_profile="codex-upgrade-qualification",
                model=config.canary_model,
                reasoning_effort=config.canary_reasoning_effort,
            )
            return ReleaseDiscoveryResult(
                provider_id="codex",
                active_version=active_version,
                active_digest_sha256=active_digest,
                discovered_at=discovered_at,
                candidate=staged.descriptor,
            )
        except OperationalError as exc:
            raise UpgradeManagerError(exc.code) from exc
