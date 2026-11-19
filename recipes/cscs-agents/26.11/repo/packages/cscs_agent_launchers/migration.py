#!/usr/bin/env python3
"""Consent-gated, copy-only migration of stopped historical profiles to 26.11."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from collections.abc import Callable

import migration_payload as payload
from storage import ProfileLease, atomic_json, fsync_path


ROOT_NAMES = ("config", "data", "cache", "state")
KINDS = ("opencode", "omp", "prime-agent", "paseo")
TRANSACTION_NAME = "cscs-migration-to-26.11"


class MigrationError(RuntimeError):
    """A profile cannot safely be started or copied in its current state."""


def namespace(kind: str) -> str:
    return "opencode-v2" if kind == "opencode" else kind


def profile_roots(kind: str, layout: str, host_roots: dict[str, Path],
                  project: Path, instance: str, version: str,
                  source_namespace: str | None = None) -> dict[str, Path]:
    name = source_namespace or (kind if version == "legacy" else namespace(kind))
    if layout == "home":
        stem = f"{name}-spack" if version == "legacy" else f"{name}-cscs-agents-{version}"
        return {key: base / stem for key, base in host_roots.items()}
    if layout == "project" and version != "legacy":
        base = project / ".cscs-agents" / version / name / instance
        return {key: base / key for key in ROOT_NAMES}
    raise MigrationError("Explicit or unversioned project roots have no inferred source")


def _path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise MigrationError(f"Migration paths must be absolute without '..': {path}")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise MigrationError(f"Migration path must not traverse a symlink: {ancestor}")
        if ancestor.exists() and not ancestor.is_dir():
            raise MigrationError(f"Migration root or parent is not a directory: {ancestor}")
    return path


def _roots(values: list[str] | dict[str, Path]) -> dict[str, Path]:
    if isinstance(values, dict):
        if set(values) != set(ROOT_NAMES):
            raise MigrationError("Profile roots must contain config, data, cache, and state")
        return {name: _path(values[name]) for name in ROOT_NAMES}
    return dict(zip(ROOT_NAMES, map(_path, values), strict=True))


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_jobs(jobs: list[dict]) -> None:
    if not jobs:
        raise MigrationError("Payload did not select a source profile")
    destinations = []
    sources = []
    for job in jobs:
        if job.get("kind") not in KINDS:
            raise MigrationError(f"Invalid migration profile kind: {job.get('kind')!r}")
        job["source"] = _roots(job["source"])
        job["destination"] = _roots(job["destination"])
        destinations.extend(job["destination"].values())
        sources.extend(job["source"].values())
    for index, root in enumerate(destinations):
        if any(_overlap(root, other) for other in destinations[:index]):
            raise MigrationError(f"Migration destination roots overlap: {root}")
        if any(_overlap(root, source) for source in sources):
            raise MigrationError(f"Migration source and destination overlap: {root}")


def _sidecar(root: Path, suffix: str) -> Path:
    return root.with_name(f".{root.name}.{TRANSACTION_NAME}{suffix}")


def journal_paths(jobs: list[dict]) -> list[Path]:
    return sorted({_sidecar(root, ".json") for job in jobs
                   for root in job["destination"].values()}, key=str)


def _stage_prefix(root: Path) -> str:
    return f".{root.name}.{TRANSACTION_NAME}-stage-"


def _blocked(path: Path) -> MigrationError:
    return MigrationError(
        f"Incomplete or unexplained migration state: {path}. Do not start this destination. "
        "The original source profile is still the rollback copy. Inspect the journal, listed "
        "destination/staging paths and lease owner metadata; after verifying all recorded "
        "writers are stopped, deliberately remove only the incomplete copied destination "
        "and its staging/journal/lease artifacts before retrying. No automatic recovery "
        "or destination merging is performed."
    )


def _check_transactions(jobs: list[dict], *, own_locks: set[Path] = frozenset()) -> None:
    for job in jobs:
        for root in job["destination"].values():
            if root.parent.exists():
                prefix = f".{root.name}.{TRANSACTION_NAME}"
                for path in root.parent.iterdir():
                    if path.name.startswith(prefix) and path not in own_locks:
                        raise _blocked(path)


def _empty(root: Path, owned_leases: set[Path] = frozenset()) -> bool:
    if not root.exists():
        return True
    for child in root.iterdir():
        if child.name != ".cscs-agent-storage" or child.is_symlink() or not child.is_dir():
            return False
        if any(entry not in owned_leases for entry in child.iterdir()):
            return False
    return True


def _destinations_empty(jobs: list[dict], owned_leases: set[Path] = frozenset()) -> bool:
    return all(_empty(root, owned_leases) for job in jobs
               for root in job["destination"].values())


def _source_present(kind: str, roots: dict[str, Path], source_namespace: str | None = None) -> bool:
    name = source_namespace or namespace(kind)
    pending = [roots[key] for key in ("config", "data", "state") if roots[key].exists()]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            if directory == roots["data"] / ".cscs-agent-storage" and any(
                entry.name.startswith(f"{name}{suffix}")
                for suffix in (".lease", ".maintenance.lease")
            ):
                continue
            if entry.is_symlink() or not entry.is_dir():
                return True
            pending.append(entry)
    return False


def _release_number(value: str) -> tuple[int, int] | None:
    if not re.fullmatch(r"[0-9]{2}\.[0-9]{2}", value):
        return None
    year, month = map(int, value.split("."))
    return (year, month) if 1 <= month <= 12 else None


def discover_sources(kind: str, layout: str, host_roots: dict[str, Path],
                     project: Path, instance: str) -> list[dict]:
    """Find stored profiles, independently of installed images or executables."""
    namespaces = ("opencode-v2", "opencode") if kind == "opencode" else (namespace(kind),)
    releases = set()
    if layout == "home":
        for base in host_roots.values():
            if not base.exists():
                continue
            for entry in base.iterdir():
                for name in namespaces:
                    prefix = f"{name}-cscs-agents-"
                    if entry.name.startswith(prefix):
                        releases.add((entry.name[len(prefix):], name))
        if kind != "paseo":
            releases.add(("legacy", "opencode" if kind == "opencode" else namespace(kind)))
    elif layout == "project":
        base = _path(project / ".cscs-agents")
        if base.exists():
            releases.update((entry.name, name) for entry in base.iterdir() for name in namespaces)
    else:
        raise MigrationError("Custom roots cannot discover historical source profiles")
    candidates = []
    for release, name in releases:
        number = _release_number(release)
        if release != "legacy" and (number is None or number >= (26, 11)):
            continue
        roots = _roots(profile_roots(kind, layout, host_roots, project, instance, release, name))
        if _source_present(kind, roots, name):
            candidates.append({"release": release, "namespace": name, "source": roots})
    candidates.sort(key=lambda item: (_release_number(item["release"]) or (-1, -1), item["namespace"]))
    for candidate in candidates:
        release = candidate["release"]
        ambiguous = sum(other["release"] == release for other in candidates) > 1
        candidate["selector"] = f"{release}:{candidate['namespace']}" if ambiguous else release
    return candidates


def _select_source(candidates: list[dict], selector: str | None) -> dict:
    matches = candidates
    if selector is not None:
        release, separator, name = selector.partition(":")
        matches = [candidate for candidate in candidates
                   if candidate["release"] == release and
                   (not separator or candidate["namespace"] == name)]
        if not matches:
            choices = ", ".join(candidate["selector"] for candidate in candidates) or "none"
            raise MigrationError(f"--migrate-from {selector!r} has no persistent source profile in this layout/instance; available: {choices}")
    if len(matches) == 1:
        return matches[0]
    print("Multiple historical source profiles contain persistent data; none is selected automatically:", file=sys.stderr)
    for index, candidate in enumerate(matches, 1):
        release = "unversioned legacy" if candidate["release"] == "legacy" else candidate["release"]
        print(f"  {index}. {candidate['selector']} ({release}, namespace {candidate['namespace']})", file=sys.stderr)
        for name, path in candidate["source"].items():
            print(f"       {name}: {path}", file=sys.stderr)
    choices = ", ".join(candidate["selector"] for candidate in matches)
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise MigrationError(f"Select a source explicitly with --migrate-from SELECTOR ({choices}); copying also requires --migrate-profile, or use --no-migrate-profile for fresh state")
    print("Select the source number or exact selector: ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip()
    for index, candidate in enumerate(matches, 1):
        if answer in (str(index), candidate["selector"]):
            return candidate
    raise MigrationError(f"No source selected; rerun with --migrate-from SELECTOR ({choices})")


def _preview(jobs: list[dict], lease_jobs: list[dict]) -> None:
    release = jobs[0].get("source_release", "26.10")
    label = "unversioned legacy" if release == "legacy" else release
    print(f"{label} -> 26.11 profile migration preview (copy only):", file=sys.stderr)
    top_sources = {tuple(job["source"].values()) for job in jobs}
    for job in lease_jobs:
        profile_label = "profile" if tuple(job["source"].values()) in top_sources else "nested profile"
        print(f"  {job['kind']} {profile_label}:", file=sys.stderr)
        for name in ROOT_NAMES:
            action = "empty/rebuilt" if name == "cache" else "portable payload"
            print(f"    {name}: {job['source'][name]} -> {job['destination'][name]} ({action})",
                  file=sys.stderr)
    print(
        "Included: portable sessions/history, settings, custom providers, authentication, "
        "validated immutable checkpoints, persistent bulk data and user-owned files.\n"
        "Excluded/rebuilt: cache, leases, recovery/operation metadata, PID/socket/runtime "
        "state, logs, generated plugins/context and Prime kernel-venv (path/architecture specific).\n"
        f"Stop every {label} writer, including Prime/Paseo daemons and nested harnesses, "
        "before approving. Existing leases are never stolen.\n"
        f"No second backup is created: original {label} persistent files remain unchanged "
        "as the rollback copy; temporary coordination leases are acquired and released.",
        file=sys.stderr,
    )


def _approve() -> bool:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise MigrationError(
            "A corresponding historical profile exists and the 26.11 destination is fresh. "
            "Noninteractive migration requires --migrate-profile to copy or "
            "--no-migrate-profile to start fresh; no input was consumed."
        )
    print("Copy these profiles into 26.11? [y/N] ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    if not answer:
        raise MigrationError("No migration decision received; choose --migrate-profile or --no-migrate-profile")
    answer = answer.strip().lower()
    if answer in ("y", "yes"):
        return True
    if answer in ("", "n", "no"):
        print("Starting fresh; original persistent profiles were not copied.", file=sys.stderr)
        return False
    raise MigrationError("Unrecognized migration decision; choose --migrate-profile or --no-migrate-profile")


def _mkdir(path: Path, created: set[Path]) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        created.add(directory)


def _lease_paths(job: dict, side: str) -> tuple[Path, Path]:
    base = job[side]["data"] / ".cscs-agent-storage"
    name = job.get("source_namespace", namespace(job["kind"])) if side == "source" else namespace(job["kind"])
    return base / f"{name}.lease", base / f"{name}.maintenance.lease"


def _fsync_tree(root: Path) -> None:
    for directory, _, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                continue
            if not stat.S_ISREG(mode):
                raise MigrationError(f"Nonportable staged file: {path}")
            fsync_path(path)
        fsync_path(Path(directory))


def _publish_entry(source: Path, destination: Path) -> None:
    # NFS and some Lustre installations reject RENAME_NOREPLACE. Exclusive
    # creation is portable and never merges or replaces a raced destination.
    # Hardlinks publish only our already-copied staging files, never originals.
    # The durable transaction journal blocks startup throughout partial trees.
    mode = source.lstat().st_mode
    try:
        if stat.S_ISDIR(mode):
            destination.mkdir(mode=stat.S_IMODE(mode))
            fsync_path(destination.parent)
            for child in sorted(source.iterdir(), key=lambda path: path.name):
                _publish_entry(child, destination / child.name)
            fsync_path(destination)
            source.rmdir()
        elif stat.S_ISLNK(mode):
            destination.symlink_to(os.readlink(source))
            fsync_path(destination.parent)
            source.unlink()
        elif stat.S_ISREG(mode):
            os.link(source, destination, follow_symlinks=False)
            fsync_path(destination.parent)
            source.unlink()
        else:
            raise MigrationError(f"Nonportable staged file: {source}")
    except FileExistsError as error:
        raise MigrationError(f"Destination changed during migration; refusing to overwrite: {destination}") from error
    fsync_path(source.parent)


def _write_journals(paths: list[Path], journal: dict) -> None:
    for path in paths:
        atomic_json(path, journal)


def _transaction(jobs: list[dict], lease_jobs: list[dict], scratch: Path,
                 revalidate: Callable[[], None]) -> None:
    acquired: list[ProfileLease] = []
    created: set[Path] = set()
    journal = None
    journals = journal_paths(jobs)
    owned_journals: list[Path] = []
    migration_paths = sorted({_sidecar(root, ".lease") for job in jobs
                              for root in job["destination"].values()}, key=str)
    try:
        for path in migration_paths:
            _mkdir(path.parent, created)
            lease = ProfileLease(path, "historical profile -> 26.11 migration")
            try:
                lease.acquire()
            finally:
                if lease._read_owner().get("token") == lease.token:
                    acquired.append(lease)
        _check_transactions(jobs, own_locks=set(migration_paths))
        ordinary = {path for job in lease_jobs for path in _lease_paths(job, "source")}
        ordinary.update(path for job in jobs for path in _lease_paths(job, "destination"))
        destination_leases = {path for job in jobs for path in _lease_paths(job, "destination")}
        for path in sorted(ordinary, key=str):
            _mkdir(path.parent, created)
            lease = ProfileLease(path, "migration stopped profile")
            try:
                lease.acquire()
            finally:
                if lease._read_owner().get("token") == lease.token:
                    acquired.append(lease)
        if not _destinations_empty(jobs, destination_leases):
            raise MigrationError("Copy requires empty destination roots; a destination changed or is already initialized")
        revalidate()
        token = uuid.uuid4().hex
        stages = [{name: root.with_name(f"{_stage_prefix(root)}{token}")
                   for name, root in job["destination"].items()} for job in jobs]
        journal = {
            "version": 1, "migration": f"{jobs[0].get('source_release', '26.10')} -> 26.11", "token": token,
            "owner": acquired[0].metadata, "state": "staging", "published": [],
            "jobs": [{"kind": job["kind"],
                      "source_release": job.get("source_release", "26.10"),
                      "source_namespace": job.get("source_namespace", namespace(job["kind"])),
                      "source": {name: str(path) for name, path in job["source"].items()},
                      "destination": {name: str(path) for name, path in job["destination"].items()},
                      "staging": {name: str(path) for name, path in stage.items()}}
                     for job, stage in zip(jobs, stages, strict=True)],
        }
        # An interrupted initial write is itself a blocking malformed journal.
        # Exclusive creation must not replace an unrelated marker raced in here.
        for path in journals:
            with path.open("x", encoding="utf-8") as stream:
                owned_journals.append(path)
                os.chmod(path, 0o600)
                json.dump(journal, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            fsync_path(path.parent)
        mappings = list(dict.fromkeys((job["source"][name], job["destination"][name])
                                     for job in lease_jobs for name in ROOT_NAMES))
        for job, stage in zip(jobs, stages, strict=True):
            for root in stage.values():
                root.mkdir(mode=0o700)
                fsync_path(root.parent)
            payload.copy_profile(job, stage, mappings, scratch)
            for root in stage.values():
                _fsync_tree(root)
        if not _destinations_empty(jobs, destination_leases):
            raise MigrationError("Destination changed while staging; refusing to merge copied data")
        journal["state"] = "publishing"
        _write_journals(journals, journal)
        for job, stage in zip(jobs, stages, strict=True):
            for name in ROOT_NAMES:
                destination = job["destination"][name]
                _mkdir(destination, created)
                # Empty roots may predate migration with a permissive umask.
                # Keep copied credentials private before publishing any entry.
                destination.chmod(0o700)
                for entry in sorted(stage[name].iterdir(), key=lambda path: path.name):
                    target = destination / entry.name
                    if entry.name == ".cscs-agent-storage" and target.exists():
                        # Keep the destination's live cooperative lease directories.
                        for child in sorted(entry.iterdir(), key=lambda path: path.name):
                            _publish_entry(child, target / child.name)
                            fsync_path(target)
                            fsync_path(entry)
                            journal["published"].append(str(target / child.name))
                            _write_journals(journals, journal)
                        entry.rmdir()
                    else:
                        _publish_entry(entry, target)
                        fsync_path(destination)
                        fsync_path(stage[name])
                        journal["published"].append(str(target))
                        _write_journals(journals, journal)
                fsync_path(destination)
                stage[name].rmdir()
                fsync_path(stage[name].parent)
        journal["state"] = "complete"
        _write_journals(journals, journal)
        for path in journals:
            path.unlink()
            fsync_path(path.parent)
        print(f"Copied {jobs[0].get('source_release', '26.10')} profiles into 26.11; original persistent files are unchanged.", file=sys.stderr)
    except Exception as error:
        if journal is not None:
            journal["state"] = "failed"
            journal["error"] = str(error)
            try:
                _write_journals(owned_journals, journal)
            except OSError:
                pass
            raise MigrationError(f"Migration did not complete: {error}. {_blocked(journals[0])}") from error
        raise
    finally:
        release_errors = []
        for lease in reversed(acquired):
            try:
                lease.release()
            except (OSError, RuntimeError) as error:
                release_errors.append(f"{lease.path}: {error}")
        # Remove only empty directories this process created for coordination.
        for directory in sorted(created, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        if release_errors:
            raise MigrationError("Could not release migration leases: " + "; ".join(release_errors))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--kind", choices=KINDS, required=True)
    result.add_argument("--policy", choices=("auto", "copy", "fresh"), required=True)
    result.add_argument("--layout", choices=("home", "project", "explicit"), required=True)
    result.add_argument("--project", required=True)
    result.add_argument("--instance", default="default")
    result.add_argument("--host-roots", nargs=4, metavar=("CONFIG", "DATA", "CACHE", "STATE"), required=True)
    result.add_argument("--roots", nargs=4, metavar=("CONFIG", "DATA", "CACHE", "STATE"), required=True)
    result.add_argument("--scratch", required=True)
    result.add_argument("--harness-profile", help="Explicit Paseo native-history selection; otherwise preserve source selection")
    result.add_argument("--from-release", help="Stored source VERSION[:NAMESPACE] or legacy[:NAMESPACE]; selection is not consent")
    return result


def migrate(args: argparse.Namespace) -> None:
    project = _path(args.project)
    host_roots = _roots(args.host_roots)
    destination = _roots(args.roots)
    scratch = _path(args.scratch)
    if not scratch.is_dir():
        raise MigrationError(f"Node-local scratch parent must already exist: {scratch}")
    if not args.instance or args.instance in (".", "..") or "/" in args.instance:
        raise MigrationError("Invalid project profile instance")
    selected = [{"kind": args.kind, "source": destination, "destination": destination}]
    _check_transactions(selected)
    if args.layout == "explicit":
        if args.policy == "copy" or args.from_release is not None:
            raise MigrationError("Migration cannot infer a historical source for explicit custom roots; use the default home or --project-local layout")
        return
    if args.policy == "fresh" and args.from_release is None:
        return
    if not _destinations_empty(selected):
        if args.policy == "copy" or args.from_release is not None:
            raise MigrationError("--migrate-profile/--migrate-from requires empty 26.11 destination roots; existing profiles are never merged or overwritten")
        return
    expected = profile_roots(args.kind, args.layout, host_roots, project, args.instance, "26.11")
    if destination != expected:
        raise MigrationError("Selected roots do not match the home/project 26.11 layout; explicit custom roots cannot infer a migration source")
    candidates = discover_sources(args.kind, args.layout, host_roots, project, args.instance)
    if not candidates and args.from_release is None:
        if args.policy == "copy":
            print("No previous persistent profile data was found; starting fresh without a copy.", file=sys.stderr)
        return
    candidate = _select_source(candidates, args.from_release)
    if args.policy == "fresh":
        return
    source = candidate["source"]
    selected_harness = payload.selected_harness_profile(source) if args.kind == "paseo" else None
    plan_options = {"source_release": candidate["release"], "source_namespace": candidate["namespace"]}
    jobs = payload.plan_profiles(args.kind, source, destination, host_roots, project, **plan_options)
    _validate_jobs(jobs)
    lease_jobs = payload.lease_profiles(jobs)
    for job in lease_jobs:
        job["source"] = _roots(job["source"])
        job["destination"] = _roots(job["destination"])
    _check_transactions(jobs)
    if not _destinations_empty(jobs):
        raise MigrationError("A selected 26.11 profile is already initialized; migration never merges or overwrites destinations")
    if args.harness_profile is not None:
        if args.kind != "paseo":
            raise MigrationError("--harness-profile is only valid for Paseo migration")
        if args.harness_profile != selected_harness:
            raise MigrationError(f"Requested --harness-profile {args.harness_profile!r} differs from source selection {selected_harness!r}; refusing to retarget copied history")
    external_metadata = payload.external_metadata(jobs)
    _preview(jobs, lease_jobs)
    for path in external_metadata:
        print(f"  External Git metadata copied privately into Paseo data: {path}", file=sys.stderr)
    if args.policy == "auto" and not _approve():
        return

    def identity(profiles: list[dict]) -> list[tuple]:
        return sorted((job["kind"], *(str(job[side][name])
                       for side in ("source", "destination") for name in ROOT_NAMES))
                      for job in profiles)

    def revalidate() -> None:
        current = payload.plan_profiles(args.kind, source, destination, host_roots, project, **plan_options)
        _validate_jobs(current)
        current_leases = payload.lease_profiles(current)
        if (identity(current) != identity(jobs)
                or identity(current_leases) != identity(lease_jobs)
                or payload.external_metadata(current) != external_metadata):
            raise MigrationError("Source profile selection changed during approval; rerun to review the new exact migration preview")
        if (args.kind == "paseo"
                and payload.selected_harness_profile(source) != selected_harness):
            raise MigrationError("Source Paseo harness selection changed during approval; rerun migration")

    _transaction(jobs, lease_jobs, scratch, revalidate)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        migrate(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
