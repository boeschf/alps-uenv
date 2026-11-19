import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import migration
import storage


class Terminal(io.StringIO):
    def isatty(self):
        return True


class NoRead(io.StringIO):
    def readline(self, *args, **kwargs):
        raise AssertionError("migration consumed input without an interactive decision")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project"
        self.scratch = self.base / "local-scratch"
        self.project.mkdir()
        self.scratch.mkdir()
        self.host = {name: self.base / "host" / name for name in migration.ROOT_NAMES}
        for root in self.host.values():
            root.mkdir(parents=True)
        self.kind = "prime-agent"
        self.layout = "home"
        self.instance = "default"

    def roots(self, version, kind=None, layout=None, instance=None):
        return migration.profile_roots(kind or self.kind, layout or self.layout, self.host,
                                       self.project, instance or self.instance, version)

    def arguments(self, policy="auto", *, kind=None, layout=None, roots=None, extra=()):
        return ["--kind", kind or self.kind, "--policy", policy,
                "--layout", layout or self.layout, "--project", str(self.project),
                "--instance", self.instance,
                "--host-roots", *(str(self.host[name]) for name in migration.ROOT_NAMES),
                "--roots", *(str((roots or self.roots("26.11", kind, layout))[name])
                             for name in migration.ROOT_NAMES),
                "--scratch", str(self.scratch), *extra]

    def invoke(self, policy="auto", *, stdin=None, tty=False, **kwargs):
        source = NoRead() if stdin is None else stdin
        output = Terminal() if tty else io.StringIO()
        with mock.patch.object(sys, "stdin", source), mock.patch.object(sys, "stderr", output):
            result = migration.main(self.arguments(policy, **kwargs))
        return result, output.getvalue()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def populate(self, roots=None):
        roots = self.roots("26.10") if roots is None else roots
        for root in roots.values():
            root.mkdir(parents=True, exist_ok=True)
        self.write(roots["config"] / "preferences.txt", "user preference\n")
        self.write(roots["config"] / "home/.prime/agent/settings.json", '{"provider":"custom"}\n')
        self.write(roots["config"] / "home/.prime/agent/auth.json", '{"token":"fixture-secret"}\n')
        self.write(roots["data"] / "sessions/session.jsonl",
                   '{"type":"session","id":"fixture","cwd":"/fixture/project"}\n'
                   '{"type":"message","text":"original history"}\n')
        self.write(roots["state"] / "persistent.txt", "durable state\n")
        self.write(roots["cache"] / "discard.txt", "regenerate\n")
        return roots

    def snapshot(self, roots):
        result = {}
        for name, root in roots.items():
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                key = (name, str(path.relative_to(root)))
                info = path.lstat()
                if path.is_symlink():
                    result[key] = ("symlink", os.readlink(path), info.st_ino, info.st_mtime_ns)
                elif path.is_file():
                    result[key] = ("file", path.read_bytes(), info.st_ino,
                                   info.st_mtime_ns, stat.S_IMODE(info.st_mode))
                else:
                    result[key] = ("directory", stat.S_IMODE(info.st_mode))
        return result

    def jobs(self, source=None, destination=None, kind=None):
        return [{"kind": kind or self.kind, "source": source or self.roots("26.10"),
                 "destination": destination or self.roots("26.11")}]

    def assert_no_leases(self, jobs):
        for job in jobs:
            for path in migration._lease_paths(job, "source"):
                self.assertFalse(os.path.lexists(path), str(path))
                self.assertFalse(os.path.lexists(path.with_name(path.name + ".operation")), str(path))
            for path in migration._lease_paths(job, "destination"):
                self.assertFalse(os.path.lexists(path), str(path))

    def test_headless_auto_fails_without_reading_input_or_initializing(self):
        source = self.populate()
        before = self.snapshot(source)
        result, output = self.invoke()
        self.assertEqual(result, 1)
        self.assertIn("--migrate-profile", output)
        self.assertIn("--no-migrate-profile", output)
        self.assertIn("no input was consumed", output)
        self.assertEqual(self.snapshot(source), before)
        self.assertTrue(all(not root.exists() for root in self.roots("26.11").values()))

    def test_both_stdin_and_stderr_must_be_terminals(self):
        self.populate()
        for stdin, tty in ((Terminal("yes\n"), False), (NoRead(), True)):
            with self.subTest(tty=tty):
                result, output = self.invoke(stdin=stdin, tty=tty)
                self.assertEqual(result, 1)
                self.assertIn("Noninteractive", output)
        self.assertTrue(all(not root.exists() for root in self.roots("26.11").values()))

    def test_decline_and_fresh_leave_original_and_destination_untouched(self):
        source = self.populate()
        before = self.snapshot(source)
        for decision in ("n\n", "no\n", "\n"):
            with self.subTest(decision=decision):
                result, output = self.invoke(stdin=Terminal(decision), tty=True)
                self.assertEqual(result, 0, output)
                self.assertIn("Starting fresh", output)
                self.assertEqual(self.snapshot(source), before)
                self.assertTrue(all(not root.exists() for root in self.roots("26.11").values()))
        result, output = self.invoke("fresh")
        self.assertEqual((result, output), (0, ""))
        self.assertEqual(self.snapshot(source), before)

    def test_closed_or_invalid_terminal_decision_is_not_consent(self):
        self.populate()
        for decision in ("", "perhaps\n"):
            with self.subTest(decision=decision):
                result, output = self.invoke(stdin=Terminal(decision), tty=True)
                self.assertEqual(result, 1)
                self.assertNotIn("Copied 26.10 profiles", output)
                self.assertTrue(all(not root.exists() for root in self.roots("26.11").values()))

    def test_approval_copies_real_payload_and_repeat_launch_skips_prompt(self):
        source = self.populate()
        before = self.snapshot(source)
        # Launcher path normalization may have already created empty roots.
        destination = self.roots("26.11")
        for root in destination.values():
            root.mkdir(parents=True)
        result, output = self.invoke(stdin=Terminal("yes\n"), tty=True)
        self.assertEqual(result, 0, output)
        for name in ("config", "data", "state"):
            for path in source[name].rglob("*"):
                if path.is_file():
                    copied = destination[name] / path.relative_to(source[name])
                    self.assertEqual(copied.read_bytes(), path.read_bytes())
                    self.assertNotEqual(copied.stat().st_ino, path.stat().st_ino)
        self.assertFalse((destination["cache"] / "discard.txt").exists())
        self.assertEqual(self.snapshot(source), before)
        for root in destination.values():
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assert_no_leases(self.jobs())
        self.assertTrue(all(not path.exists() for path in migration.journal_paths(self.jobs())))
        for name in migration.ROOT_NAMES:
            self.assertIn(f"{source[name]} -> {destination[name]}", output)
        after = self.snapshot(destination)
        result, output = self.invoke()
        self.assertEqual((result, output), (0, ""))
        self.assertEqual(self.snapshot(destination), after)

    def test_explicit_copy_is_already_approved_and_works_headlessly(self):
        source = self.populate()
        before = self.snapshot(source)
        result, output = self.invoke("copy")
        self.assertEqual(result, 0, output)
        self.assertNotIn("[y/N]", output)
        self.assertEqual((self.roots("26.11")["config"] / "preferences.txt").read_text(), "user preference\n")
        self.assertEqual(self.snapshot(source), before)

    def test_existing_destination_auto_skips_but_explicit_copy_refuses(self):
        source = self.populate()
        target = self.roots("26.11")
        self.write(target["data"] / "existing.txt", "already initialized\n")
        before = self.snapshot(target)
        result, output = self.invoke()
        self.assertEqual((result, output), (0, ""))
        result, output = self.invoke("copy")
        self.assertEqual(result, 1)
        self.assertIn("never merged or overwritten", output)
        self.assertEqual(self.snapshot(target), before)
        self.assertFalse((source["data"] / ".cscs-agent-storage").exists())

    def test_unknown_storage_metadata_is_not_an_empty_destination(self):
        self.populate()
        self.write(self.roots("26.11")["data"] / ".cscs-agent-storage/unrecognized", "do not discard")
        result, output = self.invoke("copy")
        self.assertEqual(result, 1)
        self.assertIn("empty 26.11 destination", output)

    def test_absent_source_starts_fresh_without_old_installation(self):
        self.assertEqual(self.invoke(), (0, ""))
        result, output = self.invoke("copy")
        self.assertEqual(result, 0)
        self.assertIn("No previous persistent profile data", output)
        self.assertTrue(all(not root.exists() for root in self.roots("26.11").values()))

    def test_custom_roots_preserve_reuse_without_guessing_a_source(self):
        self.populate()
        custom = {name: self.base / "custom" / name for name in migration.ROOT_NAMES}
        self.assertEqual(self.invoke(layout="explicit", roots=custom), (0, ""))
        self.assertEqual(self.invoke("fresh", layout="explicit", roots=custom), (0, ""))
        result, output = self.invoke("copy", layout="explicit", roots=custom)
        self.assertEqual(result, 1)
        self.assertIn("cannot infer a historical source", output)
        self.assertTrue(all(not root.exists() for root in custom.values()))

    def test_project_source_is_same_instance_and_never_home(self):
        self.populate()
        self.layout = "project"
        self.instance = "named-instance"
        self.assertEqual(self.invoke(), (0, ""))
        project_source = self.populate(self.roots("26.10"))
        result, output = self.invoke("copy")
        self.assertEqual(result, 0, output)
        self.assertIn(str(project_source["data"]), output)
        self.assertNotIn(str(self.host["data"] / "prime-agent-cscs-agents-26.10"), output)
        self.assertEqual((self.roots("26.11")["config"] / "preferences.txt").read_text(), "user preference\n")

    def test_opencode_v1_and_v2_same_release_require_namespace_selection(self):
        self.kind = "opencode"
        legacy = {name: root / "opencode-cscs-agents-26.10" for name, root in self.host.items()}
        self.write(legacy["config"] / "preferences.txt", "v1 config")
        self.write(self.roots("26.10")["config"] / "preferences.txt", "v2 config")
        result, output = self.invoke("copy")
        self.assertEqual(result, 1)
        self.assertIn("26.10:opencode", output)
        self.assertIn("26.10:opencode-v2", output)
        result, output = self.invoke("copy", extra=("--from-release", "26.10:opencode-v2"))
        self.assertEqual(result, 0, output)
        self.assertIn("opencode-v2-cscs-agents-26.10", output)
        self.assertEqual((self.roots("26.11")["config"] / "preferences.txt").read_text(), "v2 config")

    def test_active_source_writer_is_rejected_and_not_recovered(self):
        source = self.populate()
        path = migration._lease_paths(self.jobs()[0], "source")[0]
        lease = storage.ProfileLease(path, "fixture old writer")
        lease.acquire()
        owner = (path / "owner.json").read_bytes()
        try:
            result, output = self.invoke("copy")
            self.assertEqual(result, 1)
            self.assertIn("already owned (active)", output)
            self.assertEqual((path / "owner.json").read_bytes(), owner)
            self.assertFalse((source["data"] / ".cscs-agent-storage/prime-agent.maintenance.lease").exists())
            self.assertFalse((self.roots("26.11")["config"] / "preferences.txt").exists())
        finally:
            lease.release()

    def test_unknown_source_writer_maintenance_operation_and_recovery_block(self):
        self.populate()
        writer, maintenance = migration._lease_paths(self.jobs()[0], "source")
        for path in (writer, maintenance, writer.with_name(writer.name + ".operation"),
                     writer.with_name(writer.name + ".recovery")):
            with self.subTest(path=path.name):
                self.write(path / "owner.json", '{"hostname":"unknown-node","pid":1}\n')
                original = (path / "owner.json").read_bytes()
                result, output = self.invoke("copy")
                self.assertEqual(result, 1)
                self.assertTrue("unknown" in output or "recovery" in output, output)
                self.assertEqual((path / "owner.json").read_bytes(), original)
                (path / "owner.json").unlink()
                path.rmdir()
        self.assert_no_leases(self.jobs())

    def test_malformed_journal_blocks_even_fresh_and_existing_profile(self):
        target = self.roots("26.11")
        self.write(target["data"] / "initialized.txt", "partial publication")
        journal = migration.journal_paths(self.jobs())[0]
        self.write(journal, "{malformed transaction")
        for policy in ("auto", "copy", "fresh"):
            with self.subTest(policy=policy):
                result, output = self.invoke(policy)
                self.assertEqual(result, 1)
                self.assertIn(str(journal), output)
                self.assertIn("No automatic recovery", output)
        self.assertEqual(journal.read_text(), "{malformed transaction")

    def test_orphan_stage_and_migration_operation_lease_block(self):
        target = self.roots("26.11")["data"]
        target.parent.mkdir(parents=True, exist_ok=True)
        artifacts = [target.with_name(migration._stage_prefix(target) + "orphan"),
                     migration._sidecar(target, ".lease.operation")]
        for path in artifacts:
            with self.subTest(path=path.name):
                path.mkdir()
                result, output = self.invoke("fresh")
                self.assertEqual(result, 1)
                self.assertIn(str(path), output)
                self.assertTrue(path.is_dir())
                path.rmdir()

    def test_failed_payload_validation_is_not_success_and_releases_source_leases(self):
        source = self.populate()
        self.write(source["data"] / "broken.db", "not a database")
        before = self.snapshot(source)
        result, output = self.invoke("copy")
        self.assertEqual(result, 1)
        self.assertIn("Migration did not complete", output)
        self.assertNotIn("Copied 26.10 profiles", output)
        self.assert_no_leases(self.jobs())
        self.assertEqual(self.snapshot(source), before)
        journals = migration.journal_paths(self.jobs())
        self.assertTrue(all(path.exists() for path in journals))
        self.assertEqual(json.loads(journals[0].read_text())["state"], "failed")
        result, output = self.invoke("fresh")
        self.assertEqual(result, 1)
        self.assertIn("Incomplete or unexplained", output)

    def test_interrupted_publication_blocks_normal_repeat_and_preserves_source(self):
        source = self.populate()
        before = self.snapshot(source)
        publish = migration._publish_entry
        moved = []

        def interrupt(source_path, destination_path):
            publish(source_path, destination_path)
            moved.append(destination_path)
            raise SystemExit(99)  # Simulate death between rename and journal update.

        with mock.patch.object(migration, "_publish_entry", side_effect=interrupt):
            with self.assertRaises(SystemExit):
                self.invoke("copy")
        self.assertEqual(len(moved), 1)
        self.assertTrue(moved[0].exists())
        self.assertEqual(self.snapshot(source), before)
        self.assert_no_leases(self.jobs())
        journal = json.loads(migration.journal_paths(self.jobs())[0].read_text())
        self.assertEqual(journal["state"], "publishing")
        self.assertIn("staging", journal["jobs"][0])
        for policy in ("auto", "fresh", "copy"):
            with self.subTest(policy=policy):
                result, output = self.invoke(policy)
                self.assertEqual(result, 1)
                self.assertIn("Do not start this destination", output)

    def test_publication_never_overwrites_a_concurrent_destination_file(self):
        source = self.populate()
        publish = migration._publish_entry
        raced = []

        def race(source_path, destination_path):
            if destination_path.name == "preferences.txt":
                destination_path.write_text("concurrent user content\n")
                raced.append(destination_path)
            publish(source_path, destination_path)

        with mock.patch.object(migration, "_publish_entry", side_effect=race):
            result, output = self.invoke("copy")
        self.assertEqual(result, 1)
        self.assertIn("refusing to overwrite", output)
        self.assertEqual(len(raced), 1)
        self.assertEqual(raced[0].read_text(), "concurrent user content\n")
        self.assertEqual((source["config"] / "preferences.txt").read_text(), "user preference\n")
        self.assert_no_leases(self.jobs())

    def test_publication_never_merges_a_concurrent_empty_subdirectory(self):
        source = self.populate()
        publish = migration._publish_entry
        raced = []

        def race(source_path, destination_path):
            if destination_path.name == "home":
                destination_path.mkdir()
                raced.append((destination_path, destination_path.stat().st_ino))
            publish(source_path, destination_path)

        with mock.patch.object(migration, "_publish_entry", side_effect=race):
            result, output = self.invoke("copy")
        self.assertEqual(result, 1, output)
        self.assertEqual(len(raced), 1)
        directory, inode = raced[0]
        self.assertEqual(directory.stat().st_ino, inode)
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual((source["config"] / "home/.prime/agent/auth.json").read_text(), '{"token":"fixture-secret"}\n')
        self.assert_no_leases(self.jobs())

    def paseo(self, mode="isolated"):
        self.kind = "paseo"
        source = self.roots("26.10")
        self.write(source["config"] / "harness-profile.json", json.dumps({"mode": mode}))
        self.write(source["data"] / "paseo/server-id", "fixture-paseo-identity")
        return source

    def test_paseo_all_top_level_native_profiles_are_copied(self):
        source = self.paseo("home")
        old_native = {}
        for kind in ("opencode", "omp"):
            old_native[kind] = self.roots("26.10", kind=kind)
            self.write(old_native[kind]["config"] / "user.txt", f"{kind} settings")
        before = [self.snapshot(roots) for roots in [source, *old_native.values()]]
        result, output = self.invoke("copy")
        self.assertEqual(result, 0, output)
        for kind in ("opencode", "omp"):
            self.assertEqual((self.roots("26.11", kind=kind)["config"] / "user.txt").read_text(), f"{kind} settings")
            self.assertIn(str(old_native[kind]["data"]), output)
        self.assertEqual([self.snapshot(roots) for roots in [source, *old_native.values()]], before)
        self.assertEqual(json.loads((self.roots("26.11")["config"] / "harness-profile.json").read_text()), {"mode": "home"})

    def test_paseo_nonempty_native_destination_prevents_any_copy(self):
        self.paseo("home")
        conflict = self.roots("26.11", kind="omp")["data"] / "existing.txt"
        self.write(conflict, "existing native history")
        result, output = self.invoke("copy")
        self.assertEqual(result, 1)
        self.assertIn("never merges or overwrites", output)
        self.assertFalse((self.roots("26.11")["config"] / "harness-profile.json").exists())
        self.assertEqual(conflict.read_text(), "existing native history")

    def test_paseo_nested_native_writer_is_locked_and_released_on_error(self):
        source = self.paseo()
        native = {name: source["data"] / "harnesses/omp/default" / name for name in migration.ROOT_NAMES}
        self.write(native["config"] / "user.txt", "nested native settings")
        nested = self.jobs(source=native, destination={name: self.roots("26.11")["data"] / "harnesses/omp/default" / name
                                                      for name in migration.ROOT_NAMES}, kind="omp")[0]
        path = migration._lease_paths(nested, "source")[0]
        lease = storage.ProfileLease(path, "fixture nested writer")
        lease.acquire()
        try:
            result, output = self.invoke("copy")
            self.assertEqual(result, 1)
            self.assertIn("already owned (active)", output)
            self.assertTrue(path.exists())
            self.assert_no_leases(self.jobs())
        finally:
            lease.release()
        result, output = self.invoke("copy")
        self.assertEqual(result, 0, output)
        self.assertEqual((self.roots("26.11")["data"] / "harnesses/omp/default/config/user.txt").read_text(), "nested native settings")
        self.assert_no_leases([nested])

    def test_explicit_paseo_selection_mismatch_rejects_before_prompt(self):
        self.paseo("home")
        result, output = self.invoke(extra=("--harness-profile", "isolated"))
        self.assertEqual(result, 1)
        self.assertIn("refusing to retarget copied history", output)
        self.assertNotIn("[y/N]", output)
        self.assert_no_leases(self.jobs())

    def test_source_selection_is_revalidated_after_approval_before_copy(self):
        source = self.paseo()
        selector = source["config"] / "harness-profile.json"

        class ChangedSelection(Terminal):
            def readline(self, *args, **kwargs):
                selector.write_text('{"mode":"home"}')
                return "yes\n"

        result, output = self.invoke(stdin=ChangedSelection(), tty=True)
        self.assertEqual(result, 1)
        self.assertIn("selection changed during approval", output)
        self.assert_no_leases(self.jobs())
        self.assertFalse((self.roots("26.11")["config"] / "harness-profile.json").exists())
        self.assertTrue(all(not path.exists() for path in migration.journal_paths(self.jobs())))

    def test_skipped_release_migrates_old_data_not_empty_newer_profile(self):
        older = self.populate(self.roots("26.08"))
        for version in ("26.09", "26.10"):
            for root in self.roots(version).values():
                (root / "empty/nested").mkdir(parents=True)
            self.write(self.roots(version)["cache"] / "only-cache.txt", "not persistent")
        self.write(self.roots("26.12")["config"] / "preferences.txt", "future source must be ignored")
        result, output = self.invoke("copy")
        self.assertEqual(result, 0, output)
        self.assertIn("26.08 -> 26.11", output)
        self.assertNotIn("26.12", output)
        self.assertEqual((self.roots("26.11")["config"] / "preferences.txt").read_bytes(),
                         (older["config"] / "preferences.txt").read_bytes())

    def test_cache_only_and_empty_profiles_do_not_prompt(self):
        for version in ("26.01", "26.09", "26.10"):
            for root in self.roots(version).values():
                (root / "empty/nested").mkdir(parents=True)
            self.write(self.roots(version)["cache"] / "cache-only", "not history")
        self.assertEqual(self.invoke(), (0, ""))
        self.assertEqual(self.invoke("fresh"), (0, ""))

    def test_multiple_releases_require_selection_not_blind_newest_copy(self):
        old = self.populate(self.roots("26.09"))
        new = self.populate(self.roots("26.10"))
        self.write(old["config"] / "preferences.txt", "older selected")
        self.write(new["config"] / "preferences.txt", "newer not selected")
        result, output = self.invoke("copy")
        self.assertEqual(result, 1)
        self.assertIn("--migrate-from SELECTOR", output)
        for roots in (old, new):
            for root in roots.values():
                self.assertIn(str(root), output)
        self.assertTrue(all(not root.exists() for root in self.roots("26.11").values()))
        result, output = self.invoke("copy", extra=("--from-release", "26.09"))
        self.assertEqual(result, 0, output)
        self.assertEqual((self.roots("26.11")["config"] / "preferences.txt").read_text(), "older selected")

    def test_interactive_multiple_source_selection_precedes_separate_approval(self):
        self.populate(self.roots("26.09"))
        self.populate(self.roots("26.10"))
        result, output = self.invoke(stdin=Terminal("1\nno\n"), tty=True)
        self.assertEqual(result, 0, output)
        self.assertIn("Multiple historical source profiles", output)
        self.assertIn("26.09 -> 26.11", output)
        self.assertIn("Starting fresh", output)
        self.assertTrue(all(not root.exists() for root in self.roots("26.11").values()))

    def test_source_selector_does_not_implicitly_approve(self):
        self.populate()
        result, output = self.invoke(extra=("--from-release", "26.10"))
        self.assertEqual(result, 1)
        self.assertIn("--migrate-profile", output)
        self.assertTrue(all(not root.exists() for root in self.roots("26.11").values()))

    def test_explicit_missing_source_selector_never_silently_starts_fresh(self):
        for policy in ("auto", "copy", "fresh"):
            with self.subTest(policy=policy):
                result, output = self.invoke(policy, extra=("--from-release", "26.09"))
                self.assertEqual(result, 1)
                self.assertIn("has no persistent source profile", output)

    def test_known_unversioned_legacy_home_is_discovered_without_old_image(self):
        old = migration.profile_roots(self.kind, "home", self.host, self.project,
                                      "default", "legacy")
        self.populate(old)
        result, output = self.invoke("copy")
        self.assertEqual(result, 0, output)
        self.assertIn("unversioned legacy -> 26.11", output)
        self.assertIn("prime-agent-spack", output)
        self.assertNotIn("26.09 ->", output)
        self.assertEqual((self.roots("26.11")["config"] / "preferences.txt").read_text(), "user preference\n")

    def test_project_release_discovery_stays_in_selected_namespace_instance(self):
        self.layout = "project"
        self.instance = "chosen"
        older = self.populate(self.roots("26.07"))
        other = self.roots("26.09", instance="other")
        self.write(other["config"] / "preferences.txt", "other instance")
        home = self.roots("26.10", layout="home")
        self.write(home["config"] / "preferences.txt", "home history")
        result, output = self.invoke("copy")
        self.assertEqual(result, 0, output)
        self.assertIn(str(older["data"]), output)
        self.assertNotIn(str(other["config"]), output)
        self.assertNotIn(str(home["config"]), output)

    def test_legacy_opencode_uses_original_source_writer_namespace(self):
        self.kind = "opencode"
        source = migration.profile_roots(self.kind, "home", self.host, self.project,
                                         "default", "legacy")
        self.write(source["config"] / "preferences.txt", "legacy settings")
        path = source["data"] / ".cscs-agent-storage/opencode.lease"
        lease = storage.ProfileLease(path, "fixture legacy OpenCode writer")
        lease.acquire()
        try:
            result, output = self.invoke("copy", extra=("--from-release", "legacy"))
            self.assertEqual(result, 1)
            self.assertIn("already owned (active)", output)
            self.assertTrue(path.exists())
            self.assertFalse((source["data"] / ".cscs-agent-storage/opencode-v2.lease").exists())
        finally:
            lease.release()

    def test_cli_exit_statuses_gate_launch_without_new_interpreter_dependencies(self):
        self.populate()
        command = [sys.executable, str(Path(migration.__file__)), *self.arguments()]
        result = subprocess.run(command, input="yes\n", text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--migrate-profile", result.stderr)
        self.assertEqual(result.stdout, "")
        command = [sys.executable, str(Path(migration.__file__)), *self.arguments("copy")]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.roots("26.11")["config"] / "preferences.txt").exists())
        result = subprocess.run([sys.executable, str(Path(migration.__file__)), *self.arguments()],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
