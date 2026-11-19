#!/usr/bin/env python3
"""Copy-only older versioned/legacy payloads; SQLite opens node-local copies only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import shutil
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from storage import copy_regular_stable, file_inventory, import_sqlite, validate_inventory

ROOTS = ("config", "data", "cache", "state")
KINDS = ("opencode", "omp", "prime-agent", "paseo")
INSTANCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")
OMP_INSTANCE = re.compile(r"[0-9a-f]{32}\Z")


def namespace(kind):
    return "opencode-v2" if kind == "opencode" else kind


def _json(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"invalid migration metadata {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"migration metadata must be an object: {path}")
    return value


def _put_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def _job(kind, source, destination, source_release="26.10", source_namespace=None):
    if kind not in KINDS:
        raise ValueError(f"unsupported migration kind: {kind}")
    return {"kind": kind, "source": {k: Path(source[k]) for k in ROOTS},
            "destination": {k: Path(destination[k]) for k in ROOTS},
            "source_release": source_release,
            "source_namespace": source_namespace or namespace(kind)}


def _selection(job):
    path = job["source"]["config"] / "harness-profile.json"
    if path.is_symlink():
        raise RuntimeError(f"Paseo selection must not be a symlink: {path}")
    if not path.exists():
        # Old profiles without the selector are unambiguous only when no history
        # exists. Never guess ownership of an existing native handle.
        data = job["source"]["data"]
        for name in ("paseo/agents", "harnesses", ".cscs-agent-storage/paseo/generations"):
            candidate = data / name
            if candidate.exists() and any(candidate.iterdir()):
                raise RuntimeError(f"Paseo history lacks harness-profile.json: {candidate}")
        return "isolated"
    value = _json(path)
    mode = value.get("mode")
    if set(value) != {"mode"} or not isinstance(mode, str) or not (
        mode in ("isolated", "home") or
        (mode.startswith("project:") and INSTANCE.fullmatch(mode.split(":", 1)[1]))
    ):
        raise RuntimeError(f"unsupported Paseo harness selection: {path}")
    return mode


def selected_harness_profile(source):
    """Return the immutable native owner selection for a Paseo source."""
    return _selection({"source": {k: Path(source[k]) for k in ROOTS}})


def _check_roots(jobs):
    sources, destinations = [], []
    for job in jobs:
        for key in ROOTS:
            src, dst = job["source"][key], job["destination"][key]
            for path in (src, dst):
                if not path.is_absolute() or path.is_symlink():
                    raise RuntimeError(f"migration root must be an absolute non-symlink: {path}")
                # A symlinked ancestor can hide aliases and defeats disjointness.
                if path.resolve() != path:
                    raise RuntimeError(f"migration root has a symlink or noncanonical ancestor: {path}")
            sources.append(src)
            destinations.append(dst)
    for index, first in enumerate(destinations):
        for second in destinations[index + 1:]:
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise RuntimeError(f"overlapping migration destinations: {first}, {second}")
        for source in sources:
            if first.is_relative_to(source) or source.is_relative_to(first):
                raise RuntimeError(f"migration destination overlaps source: {first}, {source}")


def plan_profiles(kind, source, destination, host_roots, project, *,
                  source_release="26.10", source_namespace=None):
    """Plan the selected profile plus native owners, never merging into a profile."""
    jobs = [_job(kind, source, destination, source_release, source_namespace)]
    if kind == "paseo" and source_release in (None, "legacy"):
        raise RuntimeError("unversioned legacy profiles did not support Paseo")
    if kind == "paseo":
        mode = _selection(jobs[0])
        if mode != "isolated":
            for native in ("opencode", "omp"):
                ns = namespace(native)
                if mode == "home":
                    old = {k: Path(host_roots[k]) / f"{ns}-cscs-agents-{source_release}" for k in ROOTS}
                    new = {k: Path(host_roots[k]) / f"{ns}-cscs-agents-26.11" for k in ROOTS}
                else:
                    instance = mode.split(":", 1)[1]
                    base = Path(project) / ".cscs-agents"
                    old = {k: base / str(source_release) / ns / instance / k for k in ROOTS}
                    new = {k: base / "26.11" / ns / instance / k for k in ROOTS}
                source_ns = ns
                if native == "opencode":
                    if mode == "home":
                        legacy = {k: Path(host_roots[k]) / f"opencode-cscs-agents-{source_release}" for k in ROOTS}
                    else:
                        legacy = {k: base / str(source_release) / "opencode" / instance / k for k in ROOTS}
                    def populated(roots):
                        return any(root.is_dir() and any(p.name != ".cscs-agent-storage" for p in root.iterdir())
                                   for key, root in roots.items() if key != "cache") or any(
                            (roots["data"] / ".cscs-agent-storage" / candidate / "current").is_file()
                            for candidate in ("opencode", "opencode-v2"))
                    if populated(legacy):
                        if populated(old):
                            raise RuntimeError("Paseo has ambiguous v1/v2 external OpenCode history owners")
                        old, source_ns = legacy, "opencode"
                jobs.append(_job(native, old, new, source_release, source_ns))
    _check_roots(jobs)
    # Validate nested layout before prompting/locking, and again under leases.
    lease_profiles(jobs)
    external_metadata(jobs)
    return jobs


def _nested(job):
    if job["kind"] != "paseo":
        return []
    mode = _selection(job)
    home = job["source"]["data"] / "harnesses"
    if not home.exists():
        return []
    if home.is_symlink() or not home.is_dir():
        raise RuntimeError(f"unsafe Paseo harness directory: {home}")
    result = []
    for kind_dir in sorted(home.iterdir()):
        if kind_dir.name == "omp-sessions":
            if kind_dir.is_symlink() or not kind_dir.is_dir():
                raise RuntimeError(f"unsafe Paseo session directory: {kind_dir}")
            continue
        if kind_dir.name not in ("opencode", "omp") or kind_dir.is_symlink() or not kind_dir.is_dir():
            raise RuntimeError(f"unknown Paseo harness layout: {kind_dir}")
        if mode != "isolated" and any(kind_dir.iterdir()):
            raise RuntimeError(f"Paseo has both selected and isolated native profiles: {kind_dir}")
        for instance in sorted(kind_dir.iterdir()):
            valid = instance.name == "default" if kind_dir.name == "opencode" else (
                instance.name == "default" or OMP_INSTANCE.fullmatch(instance.name))
            if not valid or instance.is_symlink() or not instance.is_dir():
                raise RuntimeError(f"unknown Paseo isolated native profile: {instance}")
            if any(p.name not in ROOTS for p in instance.iterdir()):
                raise RuntimeError(f"unknown entries in Paseo isolated profile: {instance}")
            for key in ROOTS:
                root = instance / key
                if root.is_symlink() or root.resolve() != root:
                    raise RuntimeError(f"unsafe nested native profile root: {root}")
            destination = job["destination"]["data"] / instance.relative_to(job["source"]["data"])
            result.append(_job(kind_dir.name, {k: instance / k for k in ROOTS},
                               {k: destination / k for k in ROOTS},
                               job.get("source_release", "26.10")))
    return result


def lease_profiles(jobs):
    """Return native source owners as well as the outer Paseo writer."""
    result = []
    for job in jobs:
        result.append(job)
        result.extend(_nested(job))
    return result


def _rebase(value, mappings):
    if not isinstance(value, str) or not value.startswith("/"):
        return value
    path = Path(value)
    for source, destination in sorted(mappings, key=lambda pair: len(str(pair[0])), reverse=True):
        try:
            return str(destination / path.relative_to(source))
        except ValueError:
            pass
    return value


def _managed_exclusions(job):
    """Only remove defaults with ownership proof; edited files remain byte-for-byte."""
    root = job["source"]["config"]
    locations = {
        "opencode": ("xdg/opencode", ".opencode-bwrap", {"cscs-models": "opencode.json"}),
        "omp": ("home/.omp/agent", ".omp-bwrap", {"cscs-models": "models.yml", "config": "config.yml"}),
        "prime-agent": ("home/.prime/agent", ".prime-agent-bwrap", {"cscs-models": "models.json", "settings": "settings.json"}),
    }
    excluded = set()
    if job["kind"] not in locations:
        return excluded
    relative, prefix, targets = locations[job["kind"]]
    base = root / relative
    targets = dict(targets)
    legacy_provider = base / "extensions" / "cscs-provider.ts"
    if job["kind"] == "prime-agent" and legacy_provider.is_file() and not (base / "models.json").exists():
        targets["cscs-models"] = "extensions/cscs-provider.ts"
    for label, filename in targets.items():
        marker = base / f"{prefix}-{label}.sha256"
        target = base / filename
        if target.is_file() and not target.is_symlink() and marker.is_file() and not marker.is_symlink():
            expected = marker.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{64}", expected) and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
                excluded.update((target, marker, base / f"{prefix}-{label}.refresh"))
    markers = base / "skills" / f"{prefix}-packaged-skill-links"
    if markers.is_dir() and not markers.is_symlink():
        for marker in markers.iterdir():
            link = markers.parent / marker.name
            if marker.is_file() and not marker.is_symlink() and link.is_symlink():
                previous = marker.read_text().rstrip("\n")
                if os.readlink(link) == previous:
                    # Image links alone are regenerated. Canonical user skill
                    # links are still useful, and must survive absent images.
                    if previous.startswith("/user-tools/") or previous.startswith("/opt/"):
                        excluded.update((marker, link))
    # Pre-link-registry launchers used a marker inside each skill directory.
    # Remove only proven image links, retaining user replacements and files.
    skills = base / "skills"
    if skills.is_dir() and not skills.is_symlink():
        for skill in skills.iterdir():
            marker = skill / f"{prefix}-packaged-skill"
            if not skill.is_dir() or skill.is_symlink() or not marker.is_file() or marker.is_symlink():
                continue
            for entry in skill.iterdir():
                if entry.is_symlink():
                    resolved = os.path.normpath(str(entry.parent / os.readlink(entry)))
                    if resolved.startswith(("/user-tools/", "/opt/")):
                        excluded.add(entry)
            excluded.add(marker)
            if all(entry in excluded for entry in skill.iterdir()):
                excluded.add(skill)
    return excluded


def _excluded(job, key, relative):
    parts = relative.parts
    kind = job["kind"]
    if not parts:
        return False
    if key == "cache":
        return True
    if parts[0] == ".cscs-agent-storage":
        # Only the checkpoint store is durable; leases/recovery/operations are not.
        return len(parts) == 2 and parts[1] != namespace(kind)
    if kind == "paseo" and key == "data":
        if parts[0] == "harnesses" and len(parts) > 1 and parts[1] != "omp-sessions":
            return True  # Nested native profiles have their own transformations.
        if parts[0] == "paseo" and len(parts) > 1:
            return parts[1] in {"runtime", "plugins", "commands", "logs", "cache", "daemon.pid", "daemon.lock", "daemon.sock", "daemon.log", "paseo-context.json"}
    if kind == "prime-agent":
        if key == "data" and parts[0] == "kernel-venv":
            return True
        if key == "state" and parts[0] == "daemons":
            return True
        if key == "config" and parts[:3] == ("home", ".prime", "agent") and len(parts) > 3:
            return parts[3] in {"session-leases", "daemon-workers", "daemon.pid", "daemon.lock", "logs"}
    if kind == "omp" and key == "state" and parts[0] == "omp" and len(parts) > 1:
        return parts[1] in {"browser-profiles", "run", "ssh-control", "logs"}
    if kind == "opencode" and key == "state" and parts[0] == "opencode":
        return parts[1:2] in (("locks",), ("pty",), ("logs",)) or parts[1:3] == ("latest", "locks")
    return False


def _opencode_json_locations(connection, mappings):
    """Rebase native owner metadata, never message bodies or arbitrary JSON."""
    project_columns = {row[1] for row in connection.execute('PRAGMA table_info("project")')}
    if "sandboxes" in project_columns:
        for (raw,) in connection.execute("SELECT DISTINCT sandboxes FROM project").fetchall():
            paths = json.loads(raw)
            if isinstance(paths, list):
                changed = [_rebase(path, mappings) for path in paths]
                if changed != paths:
                    connection.execute("UPDATE project SET sandboxes=? WHERE sandboxes=?", (json.dumps(changed), raw))

    def location(value):
        if not isinstance(value, dict) or not isinstance(value.get("location"), dict):
            return False
        reference = value["location"]
        previous = reference.get("directory")
        updated = _rebase(previous, mappings)
        if updated == previous:
            return False
        reference["directory"] = updated
        return True

    # OpenCode 2.1 stores version-qualified durable event types. Projection and
    # replay metadata must agree; queued user/tool text is deliberately opaque.
    for table, column, kinds in (
        ("event", "data", ("session.created.1", "session.moved.1", "session.inbox.enqueued.1")),
        ("session_inbox", "payload", ("move",)),
    ):
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if not {"type", column}.issubset(columns):
            continue
        placeholders = ",".join("?" for _ in kinds)
        rows = connection.execute(
            f'SELECT DISTINCT type,"{column}" FROM "{table}" WHERE type IN ({placeholders})', kinds,
        ).fetchall()
        for kind, raw in rows:
            value = json.loads(raw)
            if kind == "session.inbox.enqueued.1":
                item = value.get("item") if isinstance(value, dict) else None
                changed = isinstance(item, dict) and item.get("type") == "move" and location(item.get("payload"))
            else:
                changed = location(value)
            if changed:
                connection.execute(
                    f'UPDATE "{table}" SET "{column}"=? WHERE type=? AND "{column}"=?',
                    (json.dumps(value), kind, raw),
                )


def _database(source, target, scratch, mappings, *, opencode=False):
    # import_sqlite stages beside its TARGET, so target must be node-local.
    with tempfile.TemporaryDirectory(prefix=".migration-db.", dir=scratch) as directory:
        local = Path(directory) / source.name
        import_sqlite(source, local)
        with closing(sqlite3.connect(local)) as connection, connection:
            result = connection.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise RuntimeError(f"invalid SQLite migration source {source}: {result}")
            # These are owning workspace columns, NOT message/tool text or JSON
            # blobs. Unknown columns remain untouched.
            columns_by_table = (
                ("session", ("directory",)),
                ("session_v2", ("directory",)),
                ("project", ("worktree",)),
                ("project_directory", ("directory",)),
                ("worktree", ("directory",)),
            ) if opencode else ()
            for table, columns in columns_by_table:
                names = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                for column in columns:
                    if column not in names:
                        continue
                    rows = connection.execute(f'SELECT DISTINCT "{column}" FROM "{table}"').fetchall()
                    for (value,) in rows:
                        changed = _rebase(value, mappings)
                        if changed != value:
                            connection.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?', (changed, value))
            if opencode:
                _opencode_json_locations(connection, mappings)
        copy_regular_stable(local, target)


def _copy_tree(job, key, source, target, mappings, scratch, managed, *, checkpoint=False):
    if source in managed:
        return
    relative = source.relative_to(job["source"][key])
    if not checkpoint and _excluded(job, key, relative):
        return
    mode = source.lstat().st_mode
    if stat.S_ISLNK(mode):
        raw = os.readlink(source)
        absolute = Path(os.path.normpath(str(source.parent / raw))) if not os.path.isabs(raw) else Path(raw)
        rebased = _rebase(str(absolute), mappings)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(rebased if rebased != str(absolute) else raw)
    elif stat.S_ISDIR(mode):
        target.mkdir(parents=True, exist_ok=True, mode=stat.S_IMODE(mode) | 0o700)
        for child in sorted(source.iterdir()):
            _copy_tree(job, key, child, target / child.name, mappings, scratch, managed, checkpoint=checkpoint)
    elif stat.S_ISREG(mode):
        if source.name.endswith(("-wal", "-shm", "-journal")):
            base = source.with_name(re.sub(r"-(?:wal|shm|journal)$", "", source.name))
            if base.is_file() and not base.is_symlink():
                with base.open("rb") as stream:
                    database = stream.read(16) == b"SQLite format 3\x00"
                if database or base.suffix in (".db", ".sqlite", ".sqlite3"):
                    return  # Included by the stable local SQLite import.
            elif base.suffix in (".db", ".sqlite", ".sqlite3"):
                raise RuntimeError(f"orphaned SQLite sidecar has no portable database: {source}")
        with source.open("rb") as stream:
            sqlite = stream.read(16) == b"SQLite format 3\x00"
        if sqlite or source.suffix in (".db", ".sqlite", ".sqlite3"):
            native_opencode = job["kind"] == "opencode" and source.name in (
                "opencode.db", "opencode-local.db", "opencode-stable.db") and (
                checkpoint or (key == "data" and relative.parts[0] == "opencode"))
            _database(source, target, scratch, mappings, opencode=native_opencode)
        else:
            copy_regular_stable(source, target)
            if source.parent.name in {f".{kind}-bwrap-packaged-skill-links" for kind in ("opencode", "omp", "prime-agent")}:
                link = source.parent.parent / source.name
                if link.is_symlink():
                    previous = source.read_text().rstrip("\n")
                    if os.readlink(link) == previous:
                        rebased = _rebase(previous, mappings)
                        if rebased != previous:
                            target.write_text(rebased + "\n")
    else:
        raise RuntimeError(f"nonportable special file in persistent profile: {source}")


def _checkpoints(job, stage, mappings, scratch):
    source_namespace = job.get("source_namespace", namespace(job["kind"]))
    legacy_opencode = job["kind"] == "opencode" and source_namespace == "opencode"
    source = job["source"]["data"] / ".cscs-agent-storage" / source_namespace
    target = stage["data"] / (".cscs-migration-legacy" if legacy_opencode else ".cscs-agent-storage") / source_namespace
    if not source.exists():
        return
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError(f"unsafe checkpoint store: {source}")
    if any(path.name not in ("current", "generations") for path in source.iterdir()):
        raise RuntimeError(f"unknown persistent checkpoint layout: {source}")
    generations = source / "generations"
    current = source / "current"
    if current.is_symlink() or generations.is_symlink():
        raise RuntimeError(f"unsafe checkpoint pointers: {source}")
    candidates = []
    if current.exists():
        name = current.read_text().strip()
        if name and Path(name).name == name and name not in (".", ".."):
            candidates.append(generations / name)
    if generations.is_dir():
        candidates.extend(p for p in sorted(generations.iterdir(), reverse=True)
                          if not p.name.startswith(".") and p not in candidates)
    valid, failures = [], []
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_dir() or any(p.is_symlink() for p in candidate.rglob("*")):
                raise RuntimeError("checkpoint contains a symlink or is not a directory")
            # Validate bytes before SQLite import/rebasing changes the inventory.
            manifest = _json(candidate / "manifest.json")
            if manifest.get("version") != 2 or manifest.get("kind") != job["kind"]:
                raise RuntimeError("unsupported checkpoint manifest")
            validate_inventory(candidate, manifest.get("files"))
            if job["kind"] == "opencode" and not (candidate / "opencode.db").is_file():
                raise RuntimeError("OpenCode checkpoint has no database")
            destination = target / "generations" / candidate.name
            _copy_tree(job, "data", candidate, destination, mappings, scratch, set(), checkpoint=True)
            _rewrite_metadata(job, {"data": destination}, mappings, checkpoint=True)
            manifest["files"] = file_inventory(destination)
            _put_json(destination / "manifest.json", manifest)
            valid.append(candidate.name)
        except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as error:
            # Failed staged candidates cannot be published, even with fallback.
            # Only this private, failed staging subtree is removed.
            shutil.rmtree(target / "generations" / candidate.name, ignore_errors=True)
            failures.append(f"{candidate.name}: {error}")
    if valid:
        (target / "current").write_text(valid[0] + "\n")
        if legacy_opencode:
            app = stage["data"] / "opencode"
            app.mkdir(parents=True, exist_ok=True)
            copy_regular_stable(target / "generations" / valid[0] / "opencode.db", app / "opencode.db")
    elif candidates or current.exists():
        raise RuntimeError(f"no valid {job['kind']} history checkpoint in {source}: {'; '.join(failures)}")
    elif generations.exists() and any(generations.iterdir()):
        raise RuntimeError(f"incomplete checkpoint history: {generations}")


def _field(value, key, mappings):
    if isinstance(value, dict) and key in value:
        value[key] = _rebase(value[key], mappings)


def _rewrite_metadata(job, stage, mappings, *, checkpoint=False):
    data = stage.get("data")
    if data is None:
        return
    if job["kind"] == "paseo":
        home = data / "paseo"
        config = home / "config.json"
        if config.is_file() and not config.is_symlink():
            value = _json(config)
            _field(value.get("worktrees"), "root", mappings)
            providers = value.get("agents", {}).get("providers", {})
            for provider in ("omp", "opencode"):
                entry = providers.get(provider, {})
                # prepare() regenerates these exact launcher-owned commands.
                entry.pop("command", None)
                _field(entry.get("options"), "sessionDir", mappings)
            _put_json(config, value)
        agents = home / "agents"
        if agents.exists():
            for path in agents.rglob("*.json"):
                if path.is_symlink():
                    raise RuntimeError(f"linked Paseo agent metadata is ambiguous: {path}")
                value = _json(path)
                if not all(isinstance(value.get(k), str) for k in ("id", "provider", "cwd")):
                    raise RuntimeError(f"unsupported Paseo 0.12 agent record: {path}")
                _field(value, "cwd", mappings)
                handle = value.get("persistence")
                if isinstance(handle, dict):
                    provider = handle.get("provider")
                    if provider not in ("omp", "opencode"):
                        raise RuntimeError(f"unsupported persisted Paseo provider {provider!r}: {path}")
                    if provider == "omp":
                        old = handle.get("nativeHandle")
                        new = _rebase(old, mappings)
                        if not isinstance(old, str) or not old.startswith("/") or new == old:
                            raise RuntimeError(f"OMP native handle has no copied owner: {path}")
                        if not Path(old).is_file():
                            raise RuntimeError(f"OMP native session is missing: {old}")
                        handle["nativeHandle"] = new
                    _field(handle.get("metadata"), "cwd", mappings)
                _put_json(path, value)
        for filename, fields in (("projects.json", ("rootPath",)),
                                 ("workspaces.json", ("cwd", "worktreeRoot", "mainRepoRoot"))):
            path = home / "projects" / filename
            if not path.exists():
                continue
            if path.is_symlink():
                raise RuntimeError(f"linked Paseo registry is ambiguous: {path}")
            try:
                records = json.loads(path.read_text())
            except ValueError as error:
                raise RuntimeError(f"invalid Paseo registry: {path}") from error
            if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                raise RuntimeError(f"unsupported Paseo 0.12 registry: {path}")
            for record in records:
                for field in fields:
                    _field(record, field, mappings)
            _put_json(path, records)
        schedules = home / "schedules"
        if schedules.exists():
            for path in schedules.glob("*.json"):
                if path.is_symlink():
                    raise RuntimeError(f"linked Paseo schedule is ambiguous: {path}")
                value = _json(path)
                target = value.get("target")
                if not isinstance(target, dict) or target.get("type") not in ("agent", "new-agent"):
                    raise RuntimeError(f"unsupported Paseo 0.12 schedule: {path}")
                if target["type"] == "new-agent":
                    _field(target.get("config"), "cwd", mappings)
                _put_json(path, value)
    # Only session headers own these paths. Never replace conversation text,
    # tool-call strings, system prompts, provider config, or arbitrary JSON.
    if job["kind"] in ("omp", "prime-agent", "paseo") and not checkpoint:
        directories = ([data / "omp" / "sessions"] if job["kind"] == "omp" else
                       [data / "sessions"] if job["kind"] == "prime-agent" else
                       [data / "harnesses" / "omp-sessions"])
        if job["kind"] == "omp" and "config" in stage:
            directories.append(stage["config"] / "home" / ".omp" / "agent" / "sessions")
        if job["kind"] == "prime-agent" and "config" in stage:
            directories.append(stage["config"] / "home" / ".prime" / "agent" / "sessions")
            directories.append(stage["config"] / "home" / ".prime" / "agent" / "rlm-sessions")
        for directory in directories:
            if not directory.exists():
                continue
            for path in directory.rglob("*.jsonl"):
                if path.is_symlink():
                    continue
                with path.open("rb") as stream:
                    first = stream.readline()
                    try:
                        value = json.loads(first)
                    except ValueError:
                        continue  # Auxiliary JSONL, including torn ledger tails.
                    if not isinstance(value, dict) or value.get("type") != "session":
                        continue  # Only proven native session headers own paths.
                    before = dict(value)
                    for key in ("cwd", "parentSession", "parentSessionPath"):
                        _field(value, key, mappings)
                    if value != before:
                        temporary = None
                        try:
                            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".migration-session.", delete=False) as output:
                                temporary = Path(output.name)
                                output.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
                                shutil.copyfileobj(stream, output)
                            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
                            os.replace(temporary, path)
                        finally:
                            if temporary is not None:
                                temporary.unlink(missing_ok=True)
    if job["kind"] == "prime-agent" and not checkpoint:
        _prime_metadata(job, stage, mappings)


def _prime_metadata(job, stage, mappings):
    """Prime 0.10 RLM topology is metadata, unlike prompts and semantic ledgers."""
    agent = stage["config"] / "home" / ".prime" / "agent"
    for root in (stage["config"], stage["data"]):
        for path in root.rglob("rlm-subagent.json"):
            if path.is_symlink():
                raise RuntimeError(f"linked Prime child metadata is ambiguous: {path}")
            value = _json(path)
            if value.get("type") == "rlm_subagent":
                for key in ("sessionDir", "sessionFile"):
                    _field(value, key, mappings)
                _put_json(path, value)
        for path in root.rglob("rlm-subagents.jsonl"):
            if path.is_symlink():
                raise RuntimeError(f"linked Prime child registry is ambiguous: {path}")
            _prime_jsonl(path, mappings, registry=True)
    ledgers = agent / "rlm-ledger"
    if ledgers.is_symlink():
        raise RuntimeError(f"linked Prime spawn ledger is ambiguous: {ledgers}")
    if ledgers.is_dir():
        for path in list(ledgers.glob("*.jsonl")):
            if path.is_symlink():
                raise RuntimeError(f"linked Prime spawn ledger is ambiguous: {path}")
            owners = _prime_jsonl(path, mappings)
            owners.update((job["source"]["data"] / "sessions",
                           job["source"]["config"] / "home/.prime/agent/sessions"))
            matches = [owner for owner in owners
                       if hashlib.sha256(os.fsencode(owner)).hexdigest()[:16] == path.stem]
            if len(matches) != 1:
                raise RuntimeError(f"cannot identify copied Prime spawn-ledger owner: {path}")
            rebased = _rebase(str(matches[0]), mappings)
            renamed = path.with_name(hashlib.sha256(os.fsencode(rebased)).hexdigest()[:16] + ".jsonl")
            if renamed != path:
                if renamed.exists():
                    raise RuntimeError(f"Prime spawn-ledger owner collision: {renamed}")
                path.rename(renamed)


def _prime_jsonl(path, mappings, *, registry=False):
    owners = set()
    lines = []
    changed = False
    for raw in path.read_bytes().splitlines(keepends=True):
        try:
            value = json.loads(raw)
        except ValueError:
            lines.append(raw)
            continue  # Match native tolerant replay; do not repair history.
        recognized = isinstance(value, dict) and (
            (registry and isinstance(value.get("childId"), str)) or
            (not registry and value.get("v") == 1 and value.get("op") in ("spawn", "rename", "delete")))
        if not recognized:
            lines.append(raw)
            continue
        before = dict(value)
        fields = ("sessionDir", "sessionFile", "parentSessionFile") if registry else ("parent", "child")
        for key in fields:
            old = value.get(key)
            if isinstance(old, str) and old.startswith("/"):
                if key != "sessionDir":
                    owners.add(Path(old).parent)
                value[key] = _rebase(old, mappings)
        if value != before:
            lines.append(json.dumps(value, separators=(",", ":")).encode() + (b"\n" if raw.endswith(b"\n") else b""))
            changed = True
        else:
            lines.append(raw)
    if changed:
        path.write_bytes(b"".join(lines))
    return owners


def _git_target(pointer, prefix=""):
    if pointer.is_symlink() or not pointer.is_file():
        raise RuntimeError(f"unsafe Git administrative pointer: {pointer}")
    raw = pointer.read_text().strip()
    if prefix:
        if not raw.startswith(prefix):
            raise RuntimeError(f"unknown Git administrative pointer: {pointer}")
        raw = raw[len(prefix):]
    if not raw or "\n" in raw:
        raise RuntimeError(f"invalid Git administrative pointer: {pointer}")
    return (pointer.parent / raw).resolve()


def _external_git(job):
    """Discover linked Paseo worktrees and the complete external common stores."""
    if job["kind"] != "paseo":
        return []
    root = job["source"]["data"] / "paseo" / "worktrees"
    result = []
    if root.is_symlink():
        raise RuntimeError(f"Paseo worktree collection must be owned: {root}")
    for pointer in sorted(root.rglob(".git")) if root.exists() else []:
        if pointer.is_dir() and not pointer.is_symlink():
            continue
        admin = _git_target(pointer, "gitdir: ")
        if any(admin.is_relative_to(job["source"][key]) for key in ROOTS):
            continue
        common = _git_target(admin / "commondir")
        if admin.parent != common / "worktrees" or not (common / "objects").is_dir():
            raise RuntimeError(f"unsupported external Git worktree layout: {pointer}")
        if _git_target(admin / "gitdir") != pointer.resolve():
            raise RuntimeError(f"Git worktree reverse pointer disagrees: {pointer}")
        if common.stat().st_uid != os.getuid():
            raise RuntimeError(f"external Git metadata is not user-owned: {common}")
        result.append((pointer, admin, common))
    return result


def external_metadata(jobs):
    """External read-only sources that must be displayed in approval previews."""
    return sorted({common for job in jobs for _, _, common in _external_git(job)})


def _copy_git_metadata(source, target, *, common=False):
    if source.is_symlink():
        raise RuntimeError(f"Git metadata symlink cannot be safely bundled: {source}")
    if source.name.endswith(".lock") or source.name in ("alternates", "http-alternates"):
        raise RuntimeError(f"Git metadata has active locks or external object dependencies: {source}")
    mode = source.lstat().st_mode
    if stat.S_ISDIR(mode):
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in source.iterdir():
            # Unrelated working trees are not ours. Only selected admin
            # directories are copied separately below.
            if common and child.name == "worktrees":
                continue
            _copy_git_metadata(child, target / child.name)
    elif stat.S_ISREG(mode):
        copy_regular_stable(source, target)
    else:
        raise RuntimeError(f"nonportable Git metadata file: {source}")


def _git_inventory(common):
    # Detect concurrent project Git writers, which do not take profile leases.
    # file_inventory cannot distinguish symlinks, so reject those first.
    paths = [common, *common.rglob("*")]
    if any(p.is_symlink() or p.name.endswith(".lock") for p in paths):
        raise RuntimeError(f"external Git metadata is linked or locked: {common}")
    return file_inventory(common)


def _git_environment():
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    return environment


def _remove_git_worktree_override(config):
    if not config.exists():
        return
    # core.worktree is owning metadata, not a project association. A copied
    # common store must never send native Git writes to the original checkout.
    result = subprocess.run(["git", "config", "--file", str(config), "--unset-all", "core.worktree"],
                            env=_git_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode not in (0, 5):
        raise RuntimeError(f"cannot rebase copied Git configuration {config}: {result.stderr.strip()}")


def _bundle_git(job, stage, mappings):
    records = _external_git(job)
    groups = {}
    for pointer, admin, common in records:
        groups.setdefault(common, []).append((pointer, admin))
    extra = []
    for common, worktrees in groups.items():
        digest = hashlib.sha256(os.fsencode(common)).hexdigest()[:20]
        relative = Path(".cscs-migration-git") / digest
        local = stage["data"] / relative
        final = job["destination"]["data"] / relative
        if local.exists():
            raise RuntimeError(f"reserved migration Git destination already exists: {local}")
        before = _git_inventory(common)
        _copy_git_metadata(common, local, common=True)
        _remove_git_worktree_override(local / "config")
        for pointer, admin in worktrees:
            staged_admin = local / "worktrees" / admin.name
            _copy_git_metadata(admin, staged_admin)
            _remove_git_worktree_override(staged_admin / "config.worktree")
            staged_pointer = stage["data"] / pointer.relative_to(job["source"]["data"])
            final_pointer = job["destination"]["data"] / pointer.relative_to(job["source"]["data"])
            # Use staging-only common-dir while read-only Git validates the
            # copied index/objects. Never invoke Git against the old metadata.
            (staged_admin / "commondir").write_text(str(local) + "\n")
            (staged_admin / "gitdir").write_text(str(final_pointer) + "\n")
            environment = _git_environment()
            command = ["git", "--git-dir", str(staged_admin), "--work-tree", str(staged_pointer.parent)]
            for arguments in (["rev-parse", "--verify", "HEAD"], ["ls-files", "--stage"], ["fsck", "--no-reflogs", "--connectivity-only"]):
                result = subprocess.run([*command, *arguments], env=environment,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode:
                    raise RuntimeError(f"copied Git worktree validation failed for {pointer}: {result.stderr.strip()}")
            (staged_admin / "commondir").write_text(str(final) + "\n")
            staged_pointer.write_text(f"gitdir: {final / 'worktrees' / admin.name}\n")
        if _git_inventory(common) != before:
            raise RuntimeError(f"external project Git metadata changed during migration: {common}")
        extra.append((common, final))
    return [*mappings, *extra]


def _git_pointers(stage, mappings):
    for root in (stage[k] for k in ("config", "data", "state")):
        for path in root.rglob(".git"):
            if path.is_symlink():
                if "worktrees" in path.parts:
                    raise RuntimeError(f"linked worktree Git directory is unsafe: {path}")
                continue
            if not path.is_file():
                continue
            old = _git_target(path, "gitdir: ")
            new = _rebase(str(old), mappings)
            if new != str(old):
                path.write_text(f"gitdir: {new}\n")
            elif not any(old.is_relative_to(r) for r in stage.values()) and not any(
                old.is_relative_to(destination) for _, destination in mappings
            ):
                raise RuntimeError(f"external Git worktree admin must not be reused: {path}")
        for name in ("gitdir", "commondir"):
            for path in root.rglob(name):
                if path.is_symlink() or not path.is_file() or "worktrees" not in path.parts:
                    continue
                raw = path.read_text().strip()
                if os.path.isabs(raw):
                    new = _rebase(raw, mappings)
                    if new == raw and not any(Path(raw).is_relative_to(destination) for _, destination in mappings):
                        raise RuntimeError(f"external Git administrative pointer: {path}")
                    path.write_text(new + "\n")


def _legacy_opencode_database(stage):
    """Place v1 state where v2's native importer expects it, not in a v2 checkpoint."""
    app = stage["data"] / "opencode"
    imported = stage["data"] / ".cscs-migration-legacy" / "opencode" / "current"
    if imported.exists():
        return
    databases = [app / name for name in ("opencode-local.db", "opencode.db", "opencode-stable.db")
                 if (app / name).is_file()]
    if len(databases) > 1:
        raise RuntimeError(f"ambiguous legacy OpenCode database selection: {databases}")
    if databases and databases[0].name != "opencode.db":
        copy_regular_stable(databases[0], app / "opencode.db")
    if not databases and app.exists() and any(app.iterdir()):
        # Auth alone is portable, but unknown history formats must not look
        # like a successful empty database import.
        if any(path.name != "auth.json" for path in app.iterdir()):
            raise RuntimeError(f"legacy OpenCode history has no supported SQLite database: {app}")


def copy_profile(job, staging_roots, mappings, scratch):
    """Copy into fresh staging roots; persistent source files are never changed."""
    stage = {key: Path(staging_roots[key]) for key in ROOTS}
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    for path in stage.values():
        if path.is_symlink() or (path.exists() and any(path.iterdir())):
            raise RuntimeError(f"migration staging root is not empty: {path}")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    nested = _nested(job)
    mappings = [(Path(a), Path(b)) for a, b in mappings]
    mappings.extend((child["source"][key], child["destination"][key]) for child in nested for key in ROOTS)
    managed = _managed_exclusions(job)
    for key in ("config", "data", "state"):
        source = job["source"][key]
        if source.exists():
            for child in sorted(source.iterdir()):
                # Checkpoints require inventory validation before transformation.
                if key == "data" and child.name == ".cscs-agent-storage":
                    if child.is_symlink() or not child.is_dir():
                        raise RuntimeError(f"unsafe storage metadata directory: {child}")
                    for entry in child.iterdir():
                        if entry.name == job.get("source_namespace", namespace(job["kind"])):
                            continue
                        if not re.fullmatch(r"(?:opencode(?:-v2)?|omp|prime-agent|paseo)(?:\.maintenance)?\.lease(?:\..*)?", entry.name):
                            raise RuntimeError(f"unknown persistent storage payload must not be omitted: {entry}")
                    continue
                _copy_tree(job, key, child, stage[key] / child.name, mappings, scratch, managed)
    mappings = _bundle_git(job, stage, mappings)
    _checkpoints(job, stage, mappings, scratch)
    if job["kind"] == "opencode" and job.get("source_namespace") == "opencode":
        _legacy_opencode_database(stage)
    _rewrite_metadata(job, stage, mappings)
    _git_pointers(stage, mappings)
    for child in nested:
        child_stage = {key: stage["data"] / child["source"][key].relative_to(job["source"]["data"]) for key in ROOTS}
        copy_profile(child, child_stage, mappings, scratch)
