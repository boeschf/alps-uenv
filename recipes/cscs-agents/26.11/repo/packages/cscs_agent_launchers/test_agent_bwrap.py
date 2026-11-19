from __future__ import annotations

import json
import os
import shlex
import stat
import sqlite3
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).parent
RECIPE_DIR = PACKAGE_DIR.parents[2]
TEMPLATE = PACKAGE_DIR / "agent-bwrap.in"
STORAGE = PACKAGE_DIR / "storage.py"
MODEL_DEFAULTS = RECIPE_DIR / "repo/packages/cscs_agent_model_config/files"
LAUNCHER_DEFAULTS = PACKAGE_DIR / "files"


def executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class LauncherArgumentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "bwrap.json"

        fake_agent = self.bin / "agent"
        executable(fake_agent, "#!/bin/sh\nexit 0\n")
        fake_bwrap = self.bin / "bwrap"
        executable(
            fake_bwrap,
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -c "
            + shlex.quote(
                "import json,os,sys; "
                "open(os.environ['BWRAP_CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))"
            )
            + ' "$@"\n',
        )

        tools = self.root / "tools"
        self.python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        python_install = tools / f"python/{self.python_version}/cpython-test/bin"
        python_bin = tools / f"python-bin/{self.python_version}"
        uv_bin = tools / "uv/0.12.7"
        for directory in (tools / "bin", python_install, python_bin, uv_bin):
            directory.mkdir(parents=True, exist_ok=True)
        python_wrapper = python_install / "python3"
        executable(
            python_wrapper,
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n",
        )
        (python_bin / "python3").symlink_to(python_wrapper)
        (python_bin / "python").symlink_to(python_wrapper)
        executable(uv_bin / "uv", "#!/bin/sh\nprintf 'uv 0.12.7 (test)\\n'\n")
        (python_bin / ".ready").write_text("test\n")
        self.tools = tools
        self.model_generator = self.bin / "model-config"
        executable(self.model_generator, "#!/bin/sh\nexit 1\n")

        replacements = {
            "@OPENCODE@": str(fake_agent),
            "@OMP@": str(fake_agent),
            "@PRIME_AGENT@": str(fake_agent),
            "@PASEO@": str(fake_agent),
            "@PASEO_BIN@": str(self.bin),
            "@PASEO_HELPER@": str(PACKAGE_DIR / "paseo.py"),
            "@BWRAP@": str(fake_bwrap),
            "@CSCS_MODEL_CONFIG@": str(self.model_generator),
            "@CSCS_MODEL_DEFAULTS@": str(MODEL_DEFAULTS),
            "@LAUNCHER_DEFAULTS@": str(LAUNCHER_DEFAULTS),
            "@STORAGE_HELPER@": str(STORAGE),
            "@CSCS_SKILLS@": str(self.root / "no-skills"),
            "@OPENCODE_BIN@": str(self.bin),
            "@OMP_BIN@": str(self.bin),
            "@PRIME_AGENT_BIN@": str(self.bin),
            "@RIPGREP_BIN@": str(self.bin),
            "@CRUN_BIN@": str(fake_agent),
            "@CATATONIT_BIN@": str(fake_agent),
            "@FUSE_OVERLAYFS_BIN@": str(fake_agent),
        }
        launcher = TEMPLATE.read_text()
        for token, value in replacements.items():
            launcher = launcher.replace(token, value)
        self.launchers = {}
        for name in ("opencode-bwrap", "omp-bwrap", "prime-agent-bwrap", "paseo-bwrap"):
            target = self.bin / name
            executable(target, launcher)
            self.launchers[name] = target

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def launcher_invocation(
        self,
        name: str,
        command: list[str] | None = None,
        *,
        environment_overrides: dict[str, str] | None = None,
        options: list[str] | None = None,
        project_dir: Path | None = None,
        project_local: bool = False,
        instance: str | None = None,
    ) -> tuple[list[str], dict[str, str], dict[str, Path]]:
        roots = {
            key: self.root / name / key
            for key in ("project", "config", "data", "cache", "state", "local")
        }
        roots["project"].mkdir(parents=True, exist_ok=True)
        if project_dir is not None:
            roots["project"] = project_dir
            project_dir.mkdir(parents=True, exist_ok=True)
        if project_local:
            namespace = "opencode-v2" if name == "opencode-bwrap" else name.removesuffix("-bwrap")
            profile = roots["project"] / ".cscs-agents/26.11" / namespace / (instance or "default")
            for key in ("config", "data", "cache", "state"):
                roots[key] = profile / key
        environment = {
            **os.environ,
            "BWRAP_CAPTURE": str(self.capture),
            "CSCS_AGENT_LOCAL_STORAGE_DIR": str(roots["local"]),
            "CSCS_AGENT_PYTHON_VERSION": self.python_version,
            "CSCS_AGENT_TOOLS_DIR": str(self.tools),
            "CSCS_AGENT_TOOLS_CACHE_DIR": str(self.root / "tools-cache"),
            "CSCS_AGENT_SKILLS_DIR": str(self.root / "shared-data/cscs/skills"),
            "HOME": str(self.root / "home"),
        }
        for variable in (
            "CSCS_API_KEY",
            "CSCS_INFERENCE_API_KEY",
        ):
            environment.pop(variable, None)
        environment.update(environment_overrides or {})
        arguments = [
            str(self.launchers[name]),
            "--project-dir", str(roots["project"]),
            "--no-default-hide-paths",
        ]
        if project_local:
            arguments.append("--project-local")
        else:
            for key in ("config", "data", "cache", "state"):
                arguments.extend([f"--{key}-dir", str(roots[key])])
        if instance is not None:
            arguments.extend(["--instance", instance])
        arguments.extend([*(options or []), "--", *(command or [])])
        return arguments, environment, roots

    def home_profile_invocation(self, name: str, *, release: str = "26.10", options=None):
        command = ["model", "list"] if name == "prime-agent-bwrap" else ["--version"]
        arguments, environment, _ = self.launcher_invocation(name, command, options=options)
        selected = []
        iterator = iter(arguments)
        for argument in iterator:
            if argument in ("--config-dir", "--data-dir", "--cache-dir", "--state-dir"):
                next(iterator)
            else:
                selected.append(argument)
        kind = name.removesuffix("-bwrap")
        namespace = "opencode-v2" if kind == "opencode" else kind
        bases = {key: self.root / "host" / key for key in ("config", "data", "cache", "state")}
        for key, base in bases.items():
            environment[f"XDG_{key.upper()}_HOME"] = str(base)
        target = {key: base / f"{namespace}-cscs-agents-26.11" for key, base in bases.items()}
        stem = f"{kind}-spack" if release == "legacy" else f"{namespace}-cscs-agents-{release}"
        source = {key: base / stem for key, base in bases.items()}
        return selected, environment, target, source

    @staticmethod
    def invoke_profile(arguments, environment):
        return subprocess.run(
            arguments, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
        )

    def test_first_install_and_cache_only_previous_roots_start_fresh(self) -> None:
        for name in self.launchers:
            with self.subTest(harness=name):
                arguments, environment, target, source = self.home_profile_invocation(name)
                source["cache"].mkdir(parents=True)
                (source["cache"] / "discarded-cache").write_text("not persistent history")
                result = self.invoke_profile(arguments, environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(target["config"].is_dir())
                self.assertFalse(source["config"].exists())
                self.assertFalse(source["data"].exists())
                self.assertFalse((target["cache"] / "discarded-cache").exists())

    def test_headless_migration_requires_choice_and_fresh_leaves_source_untouched(self) -> None:
        arguments, environment, target, source = self.home_profile_invocation("omp-bwrap")
        auth = source["config"] / "home/.omp/agent/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"fixture-token":"preserve-original"}')
        original = auth.read_bytes()
        result = self.invoke_profile(arguments, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((target["config"] / "home/.omp/agent/auth.json").exists())
        self.assertEqual(auth.read_bytes(), original)
        arguments.insert(1, "--no-migrate-profile")
        result = self.invoke_profile(arguments, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((target["config"] / "home/.omp/agent/auth.json").exists())
        self.assertEqual(auth.read_bytes(), original)

    def assert_older_home_profile_copies(self, release: str) -> None:
        arguments, environment, target, source = self.home_profile_invocation(
            "omp-bwrap", release=release, options=["--migrate-profile"],
        )
        auth = source["config"] / "home/.omp/agent/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"fixture-token": release}))
        original = auth.read_bytes()
        result = self.invoke_profile(arguments, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target["config"] / "home/.omp/agent/auth.json").read_bytes(), original)
        self.assertEqual(auth.read_bytes(), original)

    def test_skipped_release_copies_without_old_binaries(self) -> None:
        self.assert_older_home_profile_copies("26.08")

    def test_unversioned_legacy_home_profile_copies_without_old_binaries(self) -> None:
        self.assert_older_home_profile_copies("legacy")

    def test_explicit_first_install_copy_choice_needs_no_previous_profile(self) -> None:
        arguments, environment, target, source = self.home_profile_invocation(
            "omp-bwrap", options=["--migrate-profile"],
        )
        result = self.invoke_profile(arguments, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target["config"].is_dir())
        self.assertFalse(source["config"].exists())
        self.assertFalse(source["data"].exists())

    def test_first_install_does_not_import_a_newer_profile(self) -> None:
        arguments, environment, target, source = self.home_profile_invocation(
            "omp-bwrap", release="26.12",
        )
        auth = source["config"] / "home/.omp/agent/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"fixture-token":"future-version"}')
        original = auth.read_bytes()
        result = self.invoke_profile(arguments, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((target["config"] / "home/.omp/agent/auth.json").exists())
        self.assertEqual(auth.read_bytes(), original)

    def test_multiple_versions_require_selection_and_ignore_newer_release(self) -> None:
        arguments, environment, target, source = self.home_profile_invocation(
            "omp-bwrap", release="26.08", options=["--migrate-profile"],
        )
        for version in ("26.08", "26.10", "26.12"):
            directory = source["config"].parent / f"omp-cscs-agents-{version}" / "home/.omp/agent"
            directory.mkdir(parents=True)
            (directory / "auth.json").write_text(json.dumps({"fixture-token": version}))
        result = self.invoke_profile(arguments, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((target["config"] / "home/.omp/agent/auth.json").exists())
        arguments[1:1] = ["--migrate-from", "26.08"]
        result = self.invoke_profile(arguments, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads((target["config"] / "home/.omp/agent/auth.json").read_text()),
            {"fixture-token": "26.08"},
        )

    def test_project_migration_keeps_instance_and_does_not_select_home_history(self) -> None:
        arguments, environment, roots = self.launcher_invocation(
            "omp-bwrap", ["--version"], project_local=True, instance="reviewer",
            options=["--migrate-profile"],
        )
        prior = roots["project"] / ".cscs-agents/26.08/omp/reviewer/config/home/.omp/agent"
        prior.mkdir(parents=True)
        (prior / "auth.json").write_text('{"fixture-owner":"reviewer"}')
        result = self.invoke_profile(arguments, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads((roots["config"] / "home/.omp/agent/auth.json").read_text()),
            {"fixture-owner": "reviewer"},
        )
        self.assertEqual((prior / "auth.json").read_text(), '{"fixture-owner":"reviewer"}')
        self.assertFalse((roots["project"] / ".cscs-agents/26.11/omp/default").exists())

    def test_existing_destination_is_never_merged_by_explicit_migration(self) -> None:
        arguments, environment, target, source = self.home_profile_invocation(
            "omp-bwrap", options=["--migrate-profile"],
        )
        for roots, owner in ((source, "old"), (target, "current")):
            directory = roots["config"] / "home/.omp/agent"
            directory.mkdir(parents=True)
            (directory / "auth.json").write_text(json.dumps({"fixture-owner": owner}))
        result = self.invoke_profile(arguments, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            json.loads((target["config"] / "home/.omp/agent/auth.json").read_text()),
            {"fixture-owner": "current"},
        )

    def test_paseo_migration_preserves_home_owner_and_copies_native_history_owner(self) -> None:
        arguments, environment, target, source = self.home_profile_invocation(
            "paseo-bwrap", options=["--migrate-profile"],
        )
        source["config"].mkdir(parents=True)
        selection = source["config"] / "harness-profile.json"
        selection.write_text('{"mode":"home"}')
        old_native = source["config"].parent / "omp-cscs-agents-26.10/home/.omp/agent/auth.json"
        old_native.parent.mkdir(parents=True)
        old_native.write_text('{"fixture-owner":"paseo-home"}')
        result = self.invoke_profile(arguments, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((target["config"] / "harness-profile.json").read_text()), {"mode": "home"})
        new_native = target["config"].parent / "omp-cscs-agents-26.11/home/.omp/agent/auth.json"
        self.assertEqual(new_native.read_bytes(), old_native.read_bytes())
        self.assertEqual(selection.read_text(), '{"mode":"home"}')

    def run_launcher(self, name: str, command: list[str] | None = None, **kwargs):
        arguments, environment, roots = self.launcher_invocation(name, command, **kwargs)
        subprocess.run(
            arguments, env=environment, check=True, timeout=30,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return json.loads(self.capture.read_text()), roots

    @staticmethod
    def setenv(arguments: list[str], name: str) -> str:
        for index, argument in enumerate(arguments[:-2]):
            if argument == "--setenv" and arguments[index + 1] == name:
                return arguments[index + 2]
        raise AssertionError(f"missing --setenv {name}")

    def test_skills_are_linked_offline_and_only_canonical_subtree_is_writable(self) -> None:
        packaged = self.root / "no-skills"
        packaged.symlink_to(
            RECIPE_DIR / "repo/packages/cscs_agent_skills/files", target_is_directory=True
        )
        canonical = self.root / "shared-data/cscs/skills"
        installed = canonical / "cscs-alps"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_text("---\nname: cscs-alps\n---\nOffline skill\n")
        (installed / ".digest").write_text("sha256:" + "a" * 64 + "\n")
        network_marker = self.root / "network-attempt"
        for name in ("curl", "wget"):
            executable(
                self.bin / name,
                f"#!/bin/sh\ntouch {shlex.quote(str(network_marker))}\nexit 1\n",
            )
        for launcher in ("opencode-bwrap", "omp-bwrap", "prime-agent-bwrap"):
            with self.subTest(launcher=launcher):
                arguments, roots = self.run_launcher(launcher, ["--version"])
                home = Path(self.setenv(arguments, "HOME"))
                discovery = {
                    "opencode-bwrap": roots["config"] / "xdg/opencode/skills",
                    "omp-bwrap": home / ".omp/agent/skills",
                    "prime-agent-bwrap": home / ".prime/agent/skills",
                }[launcher]
                for name in ("manage-cscs-skills", "refresh-cscs-models", "cscs-alps"):
                    self.assertTrue((discovery / name).is_symlink())
                    self.assertTrue((discovery / name / "SKILL.md").is_file())
                self.assertEqual((discovery / "cscs-alps").resolve(), installed)
                self.assertEqual(self.setenv(arguments, "CSCS_AGENT_SKILLS_DIR"), str(canonical))
                mounts = [
                    arguments[index + 1:index + 3]
                    for index, value in enumerate(arguments) if value == "--bind"
                ]
                self.assertIn([str(canonical), str(canonical)], mounts)
                alias = Path(self.setenv(arguments, "XDG_DATA_HOME")) / "cscs/skills"
                self.assertIn([str(canonical), str(alias)], mounts)
                self.assertFalse(any(source == str(canonical.parent.parent) for source, _ in mounts))
                self.assertEqual((installed / ".digest").read_text(), "sha256:" + "a" * 64 + "\n")
        self.assertFalse(network_marker.exists())

    def test_skills_repair_managed_image_links_but_preserve_user_paths(self) -> None:
        old_image = self.root / "no-skills"
        skill = old_image / "manage-cscs-skills"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("old image bootstrap\n")
        _, roots = self.run_launcher("omp-bwrap", ["--version"])
        discovery = roots["config"] / "home/.omp/agent/skills"
        managed = discovery / "manage-cscs-skills"
        self.assertTrue(managed.is_symlink())
        old_image.rename(self.root / "retired-image")
        self.assertFalse(managed.exists())
        new_image = self.root / "new-image"
        for name in ("manage-cscs-skills", "user-directory", "user-link"):
            destination = new_image / name
            destination.mkdir(parents=True)
            (destination / "SKILL.md").write_text("new image skill\n")
        user_directory = discovery / "user-directory"
        user_directory.mkdir()
        (user_directory / "SKILL.md").write_text("user content\n")
        user_link = discovery / "user-link"
        missing_user_target = self.root / "missing-user-skill"
        user_link.symlink_to(missing_user_target)
        executable(
            self.launchers["omp-bwrap"],
            self.launchers["omp-bwrap"].read_text().replace(str(old_image), str(new_image)),
        )
        self.run_launcher("omp-bwrap", ["--version"])
        self.assertEqual(managed.resolve(), new_image / "manage-cscs-skills")
        self.assertEqual((user_directory / "SKILL.md").read_text(), "user content\n")
        self.assertEqual(user_link.readlink(), missing_user_target)
        # A user replacement of a formerly managed link also relinquishes it.
        managed.unlink()
        managed.symlink_to(missing_user_target)
        self.run_launcher("omp-bwrap", ["--version"])
        self.assertEqual(managed.readlink(), missing_user_target)

    def test_skills_survive_node_local_root_change_and_nested_paseo_environment(self) -> None:
        canonical = self.root / "shared-data/cscs/skills"
        skill = canonical / "cscs-alps"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("persistent offline instructions\n")
        (skill / ".digest").write_text("sha256:" + "b" * 64 + "\n")
        outer, _ = self.run_launcher(
            "paseo-bwrap", options=["--shell"],
        )
        nested_environment = {
            name: self.setenv(outer, name)
            for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "CSCS_AGENT_SKILLS_DIR")
        }
        nested_environment["CSCS_AGENT_LOCAL_STORAGE_DIR"] = str(self.root / "another-node")
        arguments, roots = self.run_launcher(
            "omp-bwrap", ["--version"], environment_overrides=nested_environment,
        )
        self.assertEqual(self.setenv(arguments, "CSCS_AGENT_SKILLS_DIR"), str(canonical))
        discovery = roots["config"] / "home/.omp/agent/skills/cscs-alps"
        self.assertEqual(discovery.resolve(), skill)
        self.assertEqual((discovery / ".digest").read_text(), "sha256:" + "b" * 64 + "\n")
        self.assertTrue(Path(self.setenv(arguments, "XDG_DATA_HOME")).is_relative_to(
            self.root / "another-node"
        ))
        self.assertFalse((Path(nested_environment["XDG_DATA_HOME"]) / "cscs/skills/cscs-alps").exists())

    def test_skills_default_to_host_xdg_or_home_not_profile_data(self) -> None:
        for xdg in (str(self.root / "custom-host-data"), ""):
            with self.subTest(xdg=xdg):
                arguments, roots = self.run_launcher(
                    "omp-bwrap", ["--version"],
                    environment_overrides={"CSCS_AGENT_SKILLS_DIR": "", "XDG_DATA_HOME": xdg},
                )
                base = Path(xdg) if xdg else self.root / "home/.local/share"
                canonical = base / "cscs/skills"
                self.assertEqual(self.setenv(arguments, "CSCS_AGENT_SKILLS_DIR"), str(canonical))
                self.assertTrue(canonical.is_dir())
                self.assertFalse(canonical.is_relative_to(roots["data"]))


    def test_model_refresh_caches_production_and_preserves_personal_providers(self) -> None:
        calls = self.root / "model-refresh-calls"
        executable(
            self.model_generator,
            f"#!{sys.executable}\n"
            "import os, shutil, sys\n"
            "from pathlib import Path\n"
            f"calls = Path({str(calls)!r})\n"
            "with calls.open('a') as stream:\n"
            "    stream.write(str(bool(os.environ.get('CSCS_INFERENCE_API_KEY'))) + '\\n')\n"
            "out = Path(sys.argv[sys.argv.index('--out-dir') + 1])\n"
            "for filename in ('opencode.json', 'omp-models.yml', 'prime-agent-models.json'):\n"
            f"    shutil.copyfile(Path({str(MODEL_DEFAULTS)!r}) / filename, out / filename)\n",
        )
        for name, target_path, stamp_path in (
            ("opencode-bwrap", "xdg/opencode/opencode.json",
             "xdg/opencode/.opencode-bwrap-cscs-models.refresh"),
            ("omp-bwrap", "home/.omp/agent/models.yml",
             "home/.omp/agent/.omp-bwrap-cscs-models.refresh"),
            ("prime-agent-bwrap", "home/.prime/agent/models.json",
             "home/.prime/agent/.prime-agent-bwrap-cscs-models.refresh"),
        ):
            with self.subTest(harness=name):
                command = ["model", "list"] if name == "prime-agent-bwrap" else []
                kwargs = {"environment_overrides": {"CSCS_API_KEY": "test-model-key"}}
                before = len(calls.read_text().splitlines()) if calls.exists() else 0
                _, roots = self.run_launcher(name, command, **kwargs)
                stamp = roots["config"] / stamp_path
                target = roots["config"] / target_path
                self.assertEqual(stamp.read_text(), "production=1\n")
                self.assertEqual(calls.read_text().splitlines()[before:], ["True"])
                self.run_launcher(name, command, **kwargs)
                self.assertEqual(len(calls.read_text().splitlines()), before + 1)
                if target.suffix == ".yml":
                    personal = target.read_text() + "  personal:\n    baseUrl: https://personal.example/v1\n"
                else:
                    config = json.loads(target.read_text())
                    config["providers"]["personal"] = {"baseUrl": "https://personal.example/v1"}
                    personal = json.dumps(config)
                target.write_text(personal)
                stamp.unlink()
                self.run_launcher(name, command, **kwargs)
                self.assertEqual(target.read_text(), personal)
                self.assertEqual(len(calls.read_text().splitlines()), before + 1)

    def test_opencode_database_is_node_local(self) -> None:
        arguments, roots = self.run_launcher("opencode-bwrap")
        database = Path(self.setenv(arguments, "OPENCODE_DB"))
        self.assertTrue(database.is_relative_to(roots["local"]))
        self.assertFalse(database.is_relative_to(roots["data"]))
        for variable in ("CSCS_AGENT_STORAGE_CHILD", "CSCS_AGENT_MAINTENANCE_CHILD"):
            index = arguments.index(variable)
            self.assertEqual(arguments[index - 1], "--unsetenv")

    def test_omp_xdg_and_natives_are_node_local(self) -> None:
        arguments, roots = self.run_launcher("omp-bwrap")
        for variable in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            self.assertTrue(Path(self.setenv(arguments, variable)).is_relative_to(roots["local"]))
        self.assertTrue(
            Path(self.setenv(arguments, "PI_NATIVES_DIR")).is_relative_to(roots["local"])
        )
        self.assertIn("PI_CODING_AGENT_DIR", arguments)

    def test_prime_uses_models_json_and_local_runtime_overlays(self) -> None:
        arguments, roots = self.run_launcher("prime-agent-bwrap", ["model", "list"])
        agent_dir = roots["config"] / "home/.prime/agent"
        models = json.loads((agent_dir / "models.json").read_text())
        self.assertIn("cscs", models["providers"])
        self.assertFalse((agent_dir / "extensions/cscs-provider.ts").exists())
        tmpdir = Path(self.setenv(arguments, "TMPDIR"))
        self.assertIn("prime-agent", tmpdir.name)
        self.assertIn("prime-agent-", str(self.setenv(arguments, "XDG_RUNTIME_DIR")))

        for destination in (agent_dir / "session-leases", agent_dir / "daemon-workers"):
            final = arguments.index(str(destination))
            source = Path(arguments[final - 1])
            self.assertTrue(source.is_relative_to(roots["local"]))
            self.assertEqual(arguments[final - 2], "--bind")
            self.assertGreater(final, max(
                index for index, value in enumerate(arguments) if value == str(roots["config"])
            ))

    def test_network_tools_and_cache_are_rejected_before_launch(self) -> None:
        for variable, filesystem in (
            ("CSCS_AGENT_TOOLS_DIR", "nfs"),
            ("CSCS_AGENT_TOOLS_CACHE_DIR", "lustre"),
        ):
            with self.subTest(variable=variable):
                network_path = self.root / variable
                executable(
                    self.bin / "stat",
                    f"#!{sys.executable}\n"
                    "import os, sys\n"
                    f"if sys.argv[1:] == ['-f', '-c', '%T', {str(network_path)!r}]:\n"
                    f"    print({filesystem!r})\n"
                    "else:\n"
                    "    os.execv('/usr/bin/stat', ['stat', *sys.argv[1:]])\n",
                )
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.run_launcher(
                        "opencode-bwrap",
                        environment_overrides={
                            variable: str(network_path),
                            "PATH": f"{self.bin}:{os.environ['PATH']}",
                        },
                    )
                self.assertIn(f"cannot use {filesystem}", caught.exception.stderr)
                self.assertIn(str(network_path), caught.exception.stderr)
                self.assertFalse(self.capture.exists())

    def test_symlinked_tools_directory_is_rejected(self) -> None:
        link = self.root / "tools-link"
        link.symlink_to(self.tools, target_is_directory=True)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_launcher(
                "opencode-bwrap",
                environment_overrides={"CSCS_AGENT_TOOLS_DIR": str(link)},
            )
        self.assertIn("tools directory must not be a symlink", caught.exception.stderr)
        self.assertFalse(self.capture.exists())

    def test_extra_bind_cannot_mask_tools_or_cache(self) -> None:
        for target in (self.tools, self.root / "tools-cache"):
            with self.subTest(target=target):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.run_launcher(
                        "opencode-bwrap",
                        options=["--bind", f"{self.bin}:{target}"],
                    )
                self.assertIn("overlaps protected node-local", caught.exception.stderr)
                self.assertFalse(self.capture.exists())

    def test_project_instances_preserve_independent_settings(self) -> None:
        for name in self.launchers:
            with self.subTest(harness=name):
                command = ["model", "list"] if name == "prime-agent-bwrap" else []
                if name == "paseo-bwrap":
                    command = ["--version"]
                first, roots = self.run_launcher(
                    name, command, project_local=True, instance="implementer",
                )
                marker = roots["config"] / "user-settings"
                marker.write_text("implementer-only")
                second, other = self.run_launcher(
                    name, command, project_local=True, instance="reviewer",
                )
                self.assertFalse((other["config"] / "user-settings").exists())
                self.assertNotEqual(
                    self.setenv(first, "XDG_RUNTIME_DIR"),
                    self.setenv(second, "XDG_RUNTIME_DIR"),
                )
                self.run_launcher(name, command, project_local=True, instance="implementer")
                self.assertEqual(marker.read_text(), "implementer-only")

    def test_project_profile_credentials_are_git_ignored(self) -> None:
        project = self.root / "git-project"
        subprocess.run(["git", "init", "--quiet", str(project)], check=True)
        _, roots = self.run_launcher(
            "opencode-bwrap", project_dir=project, project_local=True,
        )
        credentials = roots["config"] / "credentials.json"
        credentials.write_text('{"token": "test-only"}\n')
        ignored = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "--quiet", str(credentials)],
        )
        self.assertEqual(ignored.returncode, 0)
        project_file = project / "main.py"
        project_file.write_text("pass\n")
        not_ignored = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "--quiet", str(project_file)],
        )
        self.assertEqual(not_ignored.returncode, 1)

    def test_project_profiles_do_not_follow_symlinks(self) -> None:
        project = self.root / "project-symlink"
        project.mkdir()
        elsewhere = self.root / "outside"
        elsewhere.mkdir()
        (project / ".cscs-agents").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_launcher("opencode-bwrap", project_dir=project, project_local=True)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_invalid_project_profile_options_are_rejected_without_creating_state(self) -> None:
        cases = (
            {"instance": "reviewer"},
            {"project_local": True, "instance": "../escape"},
            {"project_local": True, "instance": "/absolute"},
            {"project_local": True, "options": ["--data-dir", str(self.root / "override")]},
            {"project_local": True, "options": ["--config-dir=" + str(self.root / "override")]},
            {"options": ["--migrate-profile", "--no-migrate-profile"]},
            {"options": ["--migrate-from", "26.10", "--no-migrate-profile"]},
            {"options": ["--recover-storage", "--migrate-profile"]},
            {"options": ["--recover-storage", "--migrate-from", "26.10"]},
        )
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.run_launcher("opencode-bwrap", **options)
        self.assertFalse((self.root / "opencode-bwrap/project/.cscs-agents").exists())
        self.assertFalse((self.root / "override").exists())
        self.assertFalse(self.capture.exists())

    def test_concurrent_profiles_checkpoint_separately_and_reject_duplicate_writer(self) -> None:
        executable(
            self.bin / "bwrap",
            f"#!{sys.executable}\n"
            "import json, os, sqlite3, sys, time\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "env = {args[i+1]: args[i+2] for i, value in enumerate(args) if value == '--setenv'}\n"
            "db = Path(env['OPENCODE_DB']) if 'OPENCODE_DB' in env else Path(env['XDG_DATA_HOME'])/'omp/probe.db'\n"
            "connection = sqlite3.connect(db)\n"
            "connection.execute('create table probe(value text)')\n"
            "connection.execute('insert into probe values (?)', (os.environ['PROFILE_VALUE'],))\n"
            "connection.commit()\n"
            "Path(os.environ['PROFILE_READY']).write_text(str(db))\n"
            "while not Path(os.environ['PROFILE_RELEASE']).exists():\n"
            "    time.sleep(0.02)\n"
            "connection.close()\n",
        )
        for name in ("opencode-bwrap", "omp-bwrap"):
            with self.subTest(harness=name):
                release = self.root / f"{name}.release"
                children = []
                profiles = []
                try:
                    for index, (project, instance) in enumerate((
                        ("project-a", "implementer"),
                        ("project-a", "reviewer"),
                        ("project-b", "implementer"),
                    )):
                        ready = self.root / f"{name}-{index}.ready"
                        arguments, environment, roots = self.launcher_invocation(
                            name, project_local=True, instance=instance,
                            project_dir=self.root / project,
                            environment_overrides={
                                "PROFILE_VALUE": str(index),
                                "PROFILE_READY": str(ready),
                                "PROFILE_RELEASE": str(release),
                            },
                        )
                        child = subprocess.Popen(
                            arguments, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                        )
                        children.append(child)
                        profiles.append((roots, ready))
                        deadline = time.monotonic() + 15
                        while not ready.exists():
                            if child.poll() is not None:
                                self.fail(f"launcher exited: {child.communicate()}")
                            if time.monotonic() >= deadline:
                                self.fail("launcher did not reach the supervised writer")
                            time.sleep(0.02)

                    arguments, environment, _ = self.launcher_invocation(
                        name, project_local=True, instance="implementer",
                        project_dir=self.root / "project-a",
                    )
                    duplicate = subprocess.run(
                        arguments, env=environment, capture_output=True, text=True, timeout=10,
                    )
                    self.assertNotEqual(duplicate.returncode, 0)
                    self.assertIn("profile is already owned", duplicate.stderr)
                    self.assertTrue(all(child.poll() is None for child in children))
                    release.touch()
                    for child in children:
                        _, error = child.communicate(timeout=15)
                        self.assertEqual(child.returncode, 0, error)

                    for index, (roots, ready) in enumerate(profiles):
                        database = Path(ready.read_text())
                        relative_db = (
                            Path("opencode.db") if name == "opencode-bwrap" else Path("data/probe.db")
                        )
                        store = (
                            roots["data"] / ".cscs-agent-storage"
                            / ("opencode-v2" if name == "opencode-bwrap" else "omp")
                        )
                        generation = (store / "current").read_text().strip()
                        checkpoint = store / "generations" / generation / relative_db
                        for saved in (database, checkpoint):
                            with sqlite3.connect(saved) as connection:
                                self.assertEqual(
                                    connection.execute("select value from probe").fetchall(),
                                    [(str(index),)],
                                )
                finally:
                    release.touch()
                    for child in children:
                        if child.poll() is None:
                            child.terminate()
                        child.communicate(timeout=15)

    def test_ambient_internal_mode_is_rejected(self) -> None:
        environment = {
            **os.environ,
            "CSCS_AGENT_STORAGE_CHILD": "1",
        }
        result = subprocess.run(
            [str(self.launchers["opencode-bwrap"]), "--help"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not started by the profile storage supervisor", result.stderr)

    def test_missing_paseo_password_refuses_daemon_before_creating_profile(self) -> None:
        command, environment, roots = self.launcher_invocation("paseo-bwrap")
        environment.pop("PASEO_PASSWORD", None)
        result = subprocess.run(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(roots["data"].exists())
        self.assertFalse(self.capture.exists())

    def test_paseo_recovery_without_password_preserves_quarantined_owner(self) -> None:
        command, environment, roots = self.launcher_invocation(
            "paseo-bwrap", options=["--recover-storage"],
        )
        environment.pop("PASEO_PASSWORD", None)
        lease = roots["data"] / ".cscs-agent-storage/paseo.lease"
        lease.mkdir(parents=True)
        owner = {"hostname": "previous-host", "boot_id": "previous-boot", "pid": 999999999}
        (lease / "owner.json").write_text(json.dumps(owner))
        result = subprocess.run(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(lease.exists())
        quarantined = list(lease.parent.glob("paseo.lease.recovered.*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(json.loads((quarantined[0] / "owner.json").read_text()), owner)
        self.assertFalse(self.capture.exists())

    def test_paseo_interrupt_drains_child_before_monitor_exit_and_checkpoint(self) -> None:
        ready = self.root / "daemon-ready"
        complete = self.root / "daemon-drained"
        executable(
            self.bin / "agent",
            f"#!{sys.executable}\n"
            "import signal,time\n"
            "from pathlib import Path\n"
            "def stop(signum, frame):\n"
            "    time.sleep(0.2)\n"
            f"    Path({str(complete)!r}).write_text('drained')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGINT, stop)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            f"Path({str(ready)!r}).touch()\n"
            "while True: time.sleep(1)\n",
        )
        # Model the namespace monitor: if signalled before the daemon drains,
        # it tears down its child. Ignoring signals must survive exec.
        executable(
            self.bin / "bwrap",
            f"#!{sys.executable}\n"
            "import subprocess,sys\n"
            "child=subprocess.Popen(sys.argv[sys.argv.index('--')+1:])\n"
            "try:\n"
            "    raise SystemExit(child.wait())\n"
            "finally:\n"
            "    if child.poll() is None:\n"
            "        child.kill(); child.wait()\n",
        )
        command, environment, roots = self.launcher_invocation(
            "paseo-bwrap", environment_overrides={"PASEO_PASSWORD": "test-only"},
        )
        child = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            child.send_signal(signal.SIGINT)
            _, errors = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, errors.decode())
            self.assertEqual(complete.read_text(), "drained")
            self.assertTrue((roots["data"] / ".cscs-agent-storage/paseo/current").is_file())
            self.assertFalse((roots["data"] / ".cscs-agent-storage/paseo.lease").exists())
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=15)


if __name__ == "__main__":
    unittest.main()
