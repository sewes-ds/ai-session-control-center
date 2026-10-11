from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import stat
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_account_manager.core.upgrade_manager import CandidateBinding, CandidateDescriptor
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalError,
    atomic_write_model,
    assert_private_directory,
    clean_environment,
    file_sha256,
    run_bounded,
    safe_executable,
)


RELEASE_METADATA_URL = "https://releases.openai.com/codex/channels/latest"
RELEASE_HOST = "releases.openai.com"
ASSET_NAME = "codex-package-x86_64-unknown-linux-musl.tar.gz"
MAX_METADATA_BYTES = 256 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
VERSION_RE = re.compile(r"^codex-cli ([0-9]+\.[0-9]+\.[0-9]+)$")


class ProfilePreflight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    status: Literal["PROFILE_READY", "HUMAN_AUTH_REQUIRED", "PROFILE_INVALID"]
    profile_path: str
    binary_path: str
    binary_version: str
    status_exit_code: int | None
    owner_login_argv: list[str]


class ReleaseMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    asset_name: str
    asset_url: str
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidateProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    source: Literal["official_release", "local_known_good"]
    qualification_kind: Literal["OFFICIAL_NEWER_RELEASE", "PIPELINE_QUALIFIED_SAME_VERSION"]
    metadata_url: str
    version: str
    archive_name: str | None
    archive_sha256: str | None
    binary_path: str
    binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binary_device: int
    binary_inode: int
    binary_size: int
    binary_mtime_ns: int
    binary_ctime_ns: int
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StagedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    descriptor: CandidateDescriptor
    provenance: CandidateProvenance


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        raise OperationalError("RELEASE_REDIRECT_REJECTED")


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )


def _validated_release_url(value: str, *, code: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != RELEASE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise OperationalError(code)
    return value


def _read_http_json(url: str) -> dict[str, object]:
    _validated_release_url(url, code="RELEASE_METADATA_URL_UNSAFE")
    request = urllib.request.Request(url, headers={"User-Agent": "cus-codex-qualification/2"})
    try:
        with _opener().open(request, timeout=20) as response:
            final_url = response.geturl()
            if final_url != url:
                raise OperationalError("RELEASE_REDIRECT_REJECTED")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_METADATA_BYTES:
                raise OperationalError("RELEASE_METADATA_TOO_LARGE")
            raw = response.read(MAX_METADATA_BYTES + 1)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if isinstance(exc, OperationalError):
            raise
        raise OperationalError("RELEASE_METADATA_UNAVAILABLE") from exc
    if len(raw) > MAX_METADATA_BYTES:
        raise OperationalError("RELEASE_METADATA_TOO_LARGE")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperationalError("RELEASE_METADATA_INVALID") from exc
    if not isinstance(value, dict):
        raise OperationalError("RELEASE_METADATA_INVALID")
    return value


def discover_official_release() -> ReleaseMetadata:
    payload = _read_http_json(RELEASE_METADATA_URL)
    tag = payload.get("tag_name")
    assets = payload.get("assets")
    if not isinstance(tag, str) or not tag.startswith("rust-v") or not isinstance(assets, list):
        raise OperationalError("RELEASE_METADATA_INVALID")
    version = tag.removeprefix("rust-v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise OperationalError("RELEASE_METADATA_INVALID")
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == ASSET_NAME]
    if len(matching) != 1:
        raise OperationalError("RELEASE_ASSET_UNAVAILABLE")
    asset = matching[0]
    url = asset.get("browser_download_url")
    digest = asset.get("digest")
    if not isinstance(url, str) or not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise OperationalError("RELEASE_INTEGRITY_UNAVAILABLE")
    sha256 = digest.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise OperationalError("RELEASE_INTEGRITY_UNAVAILABLE")
    _validated_release_url(url, code="RELEASE_ASSET_URL_UNSAFE")
    return ReleaseMetadata(
        version=version,
        asset_name=ASSET_NAME,
        asset_url=url,
        archive_sha256=sha256,
    )


def _version(binary: Path, home: Path) -> str:
    executable = safe_executable(binary, code="CODEX_BINARY_UNSAFE")
    result = run_bounded(
        [str(executable), "--version"],
        cwd=home,
        env=clean_environment(home, extra={"CODEX_HOME": str(home)}),
        timeout_seconds=30,
        maximum_output=16 * 1024,
    )
    try:
        text = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise OperationalError("CODEX_VERSION_INVALID") from exc
    match = VERSION_RE.fullmatch(text)
    if result.returncode != 0 or match is None:
        raise OperationalError("CODEX_VERSION_INVALID")
    return match.group(1)


def observe_binary(binary: Path, home: Path) -> tuple[Path, str, str]:
    executable = safe_executable(binary, code="APPROVED_CODEX_BINARY_UNSAFE")
    return (
        executable,
        _version(executable, home),
        file_sha256(executable, code="APPROVED_CODEX_BINARY_UNSAFE"),
    )


def _quarantine_launcher_tmp(profile: Path, scratch: Path) -> None:
    launcher_tmp = profile / "tmp"
    try:
        metadata = launcher_tmp.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or launcher_tmp.is_symlink():
        raise OperationalError("PROFILE_INVALID")
    if metadata.st_uid != os.geteuid():
        raise OperationalError("PROFILE_INVALID")
    quarantine = Path(tempfile.mkdtemp(prefix="codex-launcher-tmp-", dir=scratch))
    quarantine.chmod(0o700)
    try:
        os.replace(launcher_tmp, quarantine / "tmp")
    except OSError as exc:
        raise OperationalError("PROFILE_INVALID") from exc


def _harden_installation_id(profile: Path) -> bool:
    """Make Codex's non-secret installation identifier private without reading it.

    Codex may create ``installation_id`` as an owner-owned regular file with
    read-only access for group/other. The rest of the profile remains subject
    to the existing fail-closed 0700/0600 policy. A writable, linked, foreign,
    or non-regular identifier is never repaired implicitly.
    """

    path = profile / "installation_id"
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            return False
        os.fchmod(descriptor, 0o600)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def _harden_generated_plugin_cache(profile: Path) -> bool:
    """Keep candidate-generated plugin cache private without reading its files."""

    plugins = profile / "plugins"
    cache = plugins / "cache"
    try:
        profile_device = profile.lstat().st_dev
        cache.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    for directory in (plugins, cache):
        try:
            metadata = directory.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_dev != profile_device
            ):
                return False
            directory.chmod(0o700)
        except OSError:
            return False
    stack = [cache]
    while stack:
        current = stack.pop()
        try:
            metadata = current.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_dev != profile_device
            ):
                return False
            if stat.S_ISDIR(metadata.st_mode):
                current.chmod(0o700)
                with os.scandir(current) as entries:
                    children = [Path(entry.path) for entry in entries]
                stack.extend(sorted(children, reverse=True))
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                current.chmod(0o700 if metadata.st_mode & 0o111 else 0o600)
            else:
                return False
        except OSError:
            return False
    return True


def profile_preflight(binary: Path, profile: Path, scratch: Path) -> ProfilePreflight:
    executable = safe_executable(binary, code="APPROVED_CODEX_BINARY_UNSAFE")
    directory = assert_private_directory(profile, create=True, code="PROFILE_INVALID")
    scratch_directory = assert_private_directory(scratch, create=True, code="PROFILE_INVALID")
    _quarantine_launcher_tmp(directory, scratch_directory)
    if not _harden_installation_id(directory):
        return ProfilePreflight(
            status="PROFILE_INVALID",
            profile_path=str(directory),
            binary_path=str(executable),
            binary_version="UNKNOWN",
            status_exit_code=None,
            owner_login_argv=[],
        )
    if not _harden_generated_plugin_cache(directory):
        return ProfilePreflight(
            status="PROFILE_INVALID",
            profile_path=str(directory),
            binary_path=str(executable),
            binary_version="UNKNOWN",
            status_exit_code=None,
            owner_login_argv=[],
        )
    for path in directory.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return ProfilePreflight(
                status="PROFILE_INVALID",
                profile_path=str(directory),
                binary_path=str(executable),
                binary_version="UNKNOWN",
                status_exit_code=None,
                owner_login_argv=[],
            )
        permissions = stat.S_IMODE(metadata.st_mode)
        if (stat.S_ISDIR(metadata.st_mode) and permissions & 0o077) or (
            stat.S_ISREG(metadata.st_mode) and permissions & 0o077
        ):
            return ProfilePreflight(
                status="PROFILE_INVALID",
                profile_path=str(directory),
                binary_path=str(executable),
                binary_version="UNKNOWN",
                status_exit_code=None,
                owner_login_argv=[],
            )
    version = _version(executable, scratch_directory)
    try:
        result = run_bounded(
            [str(executable), "login", "status"],
            cwd=scratch_directory,
            env=clean_environment(scratch_directory, extra={"CODEX_HOME": str(directory)}),
            timeout_seconds=30,
            maximum_output=16 * 1024,
        )
    finally:
        _quarantine_launcher_tmp(directory, scratch_directory)
    auth_path = directory / "auth.json"
    auth_exists = False
    if auth_path.exists():
        metadata = auth_path.lstat()
        auth_exists = (
            not auth_path.is_symlink()
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and not (stat.S_IMODE(metadata.st_mode) & 0o177)
        )
        if not auth_exists:
            status_value = "PROFILE_INVALID"
        else:
            status_value = "PROFILE_READY" if result.returncode == 0 else "PROFILE_INVALID"
    else:
        status_value = "HUMAN_AUTH_REQUIRED" if result.returncode == 1 else "PROFILE_INVALID"
    owner_argv = [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        f"HOME={scratch_directory}",
        f"CODEX_HOME={directory}",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        str(executable),
        "login",
    ]
    return ProfilePreflight(
        status=status_value,
        profile_path=str(directory),
        binary_path=str(executable),
        binary_version=version,
        status_exit_code=result.returncode,
        owner_login_argv=owner_argv,
    )


def _download(metadata: ReleaseMetadata, destination: Path) -> None:
    request = urllib.request.Request(
        metadata.asset_url, headers={"User-Agent": "cus-codex-qualification/2"}
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with _opener().open(request, timeout=60) as response, destination.open("wb") as output:
            if response.geturl() != metadata.asset_url:
                raise OperationalError("RELEASE_REDIRECT_REJECTED")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_ARCHIVE_BYTES:
                raise OperationalError("RELEASE_ARCHIVE_TOO_LARGE")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise OperationalError("RELEASE_ARCHIVE_TOO_LARGE")
                digest.update(chunk)
                output.write(chunk)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if isinstance(exc, OperationalError):
            raise
        raise OperationalError("RELEASE_DOWNLOAD_FAILED") from exc
    if digest.hexdigest() != metadata.archive_sha256:
        raise OperationalError("RELEASE_ARCHIVE_DIGEST_MISMATCH")
    destination.chmod(0o400)


def _safe_extract(archive: Path, destination: Path) -> None:
    total = 0
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise OperationalError("RELEASE_ARCHIVE_UNSAFE")
        for member in members:
            relative = Path(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise OperationalError("RELEASE_ARCHIVE_UNSAFE")
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                raise OperationalError("RELEASE_ARCHIVE_UNSAFE")
            target = destination / relative
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise OperationalError("RELEASE_ARCHIVE_UNSAFE")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o500 if member.mode & 0o111 else 0o400)


def _select_codex(root: Path, expected_version: str, scratch: Path) -> Path:
    candidates = sorted(
        path for path in root.rglob("codex") if path.is_file() and not path.is_symlink()
    )
    matching: list[Path] = []
    for candidate in candidates:
        try:
            if _version(candidate, scratch) == expected_version:
                matching.append(candidate)
        except OperationalError:
            continue
    preferred = [path for path in matching if path.parent.name == "bin"]
    selected = preferred if preferred else matching
    if len(selected) != 1:
        raise OperationalError("RELEASE_BINARY_AMBIGUOUS")
    return selected[0]


def _readonly_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            path.chmod(0o500 if metadata.st_mode & 0o111 else 0o400)
        elif stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o500)
    root.chmod(0o500)


def _binding_digest(binding: CandidateBinding) -> str:
    return hashlib.sha256(
        json.dumps(binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def stage_candidate(
    *,
    staging_root: Path,
    manager_runtime_root: Path,
    approved_binary: Path,
    cus_commit: str,
    telegram_commit: str,
    overlay_identity: str,
    gate_version: str,
    release_metadata: ReleaseMetadata | None = None,
) -> StagedCandidate:
    root = assert_private_directory(staging_root, create=True, code="CANDIDATE_ROOT_UNSAFE")
    runtime = assert_private_directory(
        manager_runtime_root, create=True, code="CANDIDATE_RUNTIME_UNSAFE"
    )
    scratch = assert_private_directory(root / "version-home", create=True, code="CANDIDATE_ROOT_UNSAFE")
    active = safe_executable(approved_binary, code="APPROVED_CODEX_BINARY_UNSAFE")
    active_version = _version(active, scratch)
    release = release_metadata or discover_official_release()
    newer = tuple(map(int, release.version.split("."))) > tuple(map(int, active_version.split(".")))
    source: Literal["official_release", "local_known_good"]
    kind: Literal["OFFICIAL_NEWER_RELEASE", "PIPELINE_QUALIFIED_SAME_VERSION"]
    archive_name: str | None = None
    archive_digest: str | None = None
    with tempfile.TemporaryDirectory(prefix=".candidate-", dir=root) as temporary_name:
        temporary = Path(temporary_name)
        materialized = temporary / "materialized"
        materialized.mkdir(mode=0o700)
        if newer:
            archive = temporary / release.asset_name
            _download(release, archive)
            _safe_extract(archive, materialized)
            selected = _select_codex(materialized, release.version, scratch)
            version = release.version
            source = "official_release"
            kind = "OFFICIAL_NEWER_RELEASE"
            archive_name = release.asset_name
            archive_digest = release.archive_sha256
        else:
            destination = materialized / "bin"
            destination.mkdir(mode=0o700)
            selected = destination / "codex"
            shutil.copyfile(active, selected)
            selected.chmod(0o500)
            version = active_version
            source = "local_known_good"
            kind = "PIPELINE_QUALIFIED_SAME_VERSION"
        binary_digest = file_sha256(selected, code="CANDIDATE_BINARY_UNSAFE")
        release_root = root / "releases" / f"{version}-{binary_digest}"
        releases = assert_private_directory(
            release_root.parent, create=True, code="CANDIDATE_ROOT_UNSAFE"
        )
        if release_root.exists():
            existing = _select_codex(release_root, version, scratch)
            if file_sha256(existing, code="CANDIDATE_BINARY_UNSAFE") != binary_digest:
                raise OperationalError("CANDIDATE_EXISTING_MISMATCH")
            candidate_binary = existing
        else:
            os.replace(materialized, release_root)
            candidate_binary = release_root / selected.relative_to(materialized)
            _readonly_tree(release_root)
            directory = os.open(releases, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    binding = CandidateBinding(
        provider_id="codex",
        version=version,
        digest_sha256=binary_digest,
        cus_commit=cus_commit,
        telegram_commit=telegram_commit,
        combined_overlay_identity=overlay_identity,
        compatibility_gate_version=gate_version,
    )
    descriptor = CandidateDescriptor(
        active_version=active_version,
        binary_path=str(candidate_binary),
        binding=binding,
    )
    metadata = candidate_binary.lstat()
    provenance = CandidateProvenance(
        source=source,
        qualification_kind=kind,
        metadata_url=RELEASE_METADATA_URL,
        version=version,
        archive_name=archive_name,
        archive_sha256=archive_digest,
        binary_path=str(candidate_binary),
        binary_sha256=binary_digest,
        binary_device=metadata.st_dev,
        binary_inode=metadata.st_ino,
        binary_size=metadata.st_size,
        binary_mtime_ns=metadata.st_mtime_ns,
        binary_ctime_ns=metadata.st_ctime_ns,
        binding_sha256=_binding_digest(binding),
    )
    candidate_path = runtime / "candidate.json"
    provenance_path = runtime / "candidate-provenance.json"
    atomic_write_model(candidate_path, descriptor)
    atomic_write_model(provenance_path, provenance)
    atomic_write_model(
        runtime / "candidate-provenance-sha256.json",
        {
            "schema_version": 1,
            "candidate_sha256": file_sha256(candidate_path, code="CANDIDATE_RECORD_UNSAFE"),
            "provenance_sha256": file_sha256(
                provenance_path, code="CANDIDATE_PROVENANCE_UNSAFE"
            ),
        },
    )
    return StagedCandidate(descriptor=descriptor, provenance=provenance)


def verify_candidate(descriptor: CandidateDescriptor, provenance_path: Path) -> CandidateProvenance:
    try:
        provenance = CandidateProvenance.model_validate_json(provenance_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise OperationalError("CANDIDATE_PROVENANCE_INVALID") from exc
    binary = safe_executable(Path(descriptor.binary_path), code="CANDIDATE_BINARY_UNSAFE")
    metadata = binary.lstat()
    identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    expected = (
        provenance.binary_device,
        provenance.binary_inode,
        provenance.binary_size,
        provenance.binary_mtime_ns,
        provenance.binary_ctime_ns,
    )
    if identity != expected:
        raise OperationalError("CANDIDATE_IDENTITY_CHANGED")
    if file_sha256(binary, code="CANDIDATE_BINARY_UNSAFE") != descriptor.binding.digest_sha256:
        raise OperationalError("CANDIDATE_DIGEST_MISMATCH")
    if provenance.binding_sha256 != _binding_digest(descriptor.binding):
        raise OperationalError("CANDIDATE_BINDING_MISMATCH")
    return provenance


def rebind_candidate_provenance(
    descriptor: CandidateDescriptor, provenance_path: Path
) -> CandidateProvenance:
    """Attest an engineering-produced integration binding to the same binary.

    Only the binding digest changes.  The executable identity and digest are
    re-observed before the provenance document is replaced atomically.
    """

    try:
        provenance = CandidateProvenance.model_validate_json(provenance_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise OperationalError("CANDIDATE_PROVENANCE_INVALID") from exc
    binary = safe_executable(Path(descriptor.binary_path), code="CANDIDATE_BINARY_UNSAFE")
    metadata = binary.lstat()
    observed = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    expected = (
        provenance.binary_device,
        provenance.binary_inode,
        provenance.binary_size,
        provenance.binary_mtime_ns,
        provenance.binary_ctime_ns,
    )
    if observed != expected or file_sha256(
        binary, code="CANDIDATE_BINARY_UNSAFE"
    ) != descriptor.binding.digest_sha256:
        raise OperationalError("CANDIDATE_IDENTITY_CHANGED")
    rebound = provenance.model_copy(
        update={"binding_sha256": _binding_digest(descriptor.binding)}
    )
    atomic_write_model(provenance_path, rebound)
    return rebound
