from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import migration_payload as payload
import storage


class MigrationPayloadTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.roots("source")
        self.destination = self.roots("destination")
        self.stage = self.roots("stage")
        self.host = self.roots("host")
        self.project = self.root / "project"
        self.project.mkdir()
        self.scratch = self.root / "local-scratch"
        self.scratch.mkdir()

    def roots(self, name):
        return {key: self.root / name / key for key in payload.ROOTS}

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def database(self, path, cwd=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
            db.execute("INSERT INTO session VALUES ('history', ?)", (str(cwd or self.project),))
            db.execute("CREATE TABLE message (text TEXT)")
            db.execute("INSERT INTO message VALUES (?)", (str(self.source['data']) + '/verbatim conversation',))
        return path

    def job(self, kind, **kwargs):
        return payload.plan_profiles(kind, self.source, self.destination, self.host, self.project, **kwargs)[0]

    def copy(self, kind, *, job=None, stage=None, mappings=None):
        job = job or self.job(kind)
        mappings = mappings or [(owner['source'][key], owner['destination'][key])
                                for owner in payload.lease_profiles([job]) for key in payload.ROOTS]
        payload.copy_profile(job, stage or self.stage, mappings, self.scratch)
        return job

    def snapshot(self, root):
        if not root.exists():
            return {}
        return {str(p.relative_to(root)): ('link', os.readlink(p)) if p.is_symlink() else
                ('file', p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in root.rglob('*') if p.is_symlink() or p.is_file()}

    def checkpoint(self, kind, name="healthy", namespace=None):
        store = self.source['data'] / '.cscs-agent-storage' / (namespace or payload.namespace(kind))
        generation = store / 'generations' / name
        generation.mkdir(parents=True)
        if kind == 'opencode':
            self.database(generation / 'opencode.db', self.source['data'] / 'workspace')
        elif kind == 'omp':
            self.database(generation / 'data' / 'agent.db')
            self.put(generation / 'state' / 'preferences.json', {'custom': True})
        else:
            self.put(generation / 'paseo' / 'config.json', {'worktrees': {'root': str(self.source['data'] / 'paseo/worktrees')}})
            self.put(generation / 'paseo' / 'daemon-keypair.json', {'privateKey': 'fixture-secret'})
            self.put(generation / 'paseo' / 'server-id', 'fixture-identity')
        self.put(generation / 'manifest.json', {'version': 2, 'kind': kind, 'files': storage.file_inventory(generation)})
        self.put(store / 'current', name + '\n')
        return store, generation

    def session(self, path, cwd=None):
        return self.put(path, json.dumps({'type': 'session', 'id': 'fixture', 'cwd': str(cwd or self.project),
                                         'parentSession': str(self.source['data'] / 'parent.jsonl')}) + '\n' +
                        json.dumps({'type': 'message', 'message': {'role': 'user', 'content': str(self.source['data'])}}) + '\n')

    def test_each_kind_preserves_settings_auth_secrets_and_source_bytes(self):
        for kind in payload.KINDS:
            with self.subTest(kind=kind):
                source = self.roots('source-' + kind)
                destination = self.roots('target-' + kind)
                stage = self.roots('staged-' + kind)
                self.put(source['config'] / 'user/settings.json', {'customProvider': 'my-provider', 'path': str(self.project)})
                auth = self.put(source['data'] / 'auth.json', {'token': 'fixture-secret'})
                auth.chmod(0o600)
                self.put(source['state'] / 'persistent-user-state', 'preserve')
                self.put(source['cache'] / 'cache-entry', 'do not copy')
                if kind == 'paseo':
                    self.put(source['config'] / 'harness-profile.json', {'mode': 'isolated'})
                before = {key: self.snapshot(root) for key, root in source.items()}
                job = payload.plan_profiles(kind, source, destination, self.host, self.project)[0]
                self.copy(kind, job=job, stage=stage)
                self.assertEqual((stage['data'] / 'auth.json').read_bytes(), auth.read_bytes())
                self.assertEqual((stage['data'] / 'auth.json').stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads((stage['config'] / 'user/settings.json').read_text())['customProvider'], 'my-provider')
                self.assertEqual(list(stage['cache'].iterdir()), [])
                self.assertEqual(before, {key: self.snapshot(root) for key, root in source.items()})

    def test_opencode_v2_owned_worktree_locations_rebase_without_editing_conversation(self):
        source = self.source["data"] / "opencode/opencode.db"
        source.parent.mkdir(parents=True)
        old = str(self.source["data"] / "worktrees/topic")
        new = str(self.destination["data"] / "worktrees/topic")
        outside = str(self.project)
        connection = sqlite3.connect(source)
        try:
            connection.executescript(
                "CREATE TABLE session_v2(id TEXT PRIMARY KEY, directory TEXT, path TEXT);"
                "CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT, sandboxes TEXT);"
                "CREATE TABLE project_directory(project_id TEXT, directory TEXT);"
                "CREATE TABLE worktree(project_id TEXT, directory TEXT);"
                "CREATE TABLE event(id TEXT PRIMARY KEY, type TEXT, data TEXT);"
                "CREATE TABLE session_inbox(id TEXT PRIMARY KEY, type TEXT, payload TEXT);"
                "CREATE TABLE session_message(id TEXT PRIMARY KEY, data TEXT);"
            )
            connection.execute("INSERT INTO session_v2 VALUES ('native', ?, 'subdir')", (old,))
            connection.execute("INSERT INTO project VALUES ('project', ?, ?)", (old, json.dumps([old, outside])))
            connection.execute("INSERT INTO project_directory VALUES ('project', ?)", (old,))
            connection.execute("INSERT INTO worktree VALUES ('project', ?)", (old,))
            location = {"directory": old, "workspaceID": "unchanged-workspace"}
            move = {"location": location, "projectID": "project"}
            events = (
                ("created", "session.created.1", {"location": location, "title": old}),
                ("moved", "session.moved.1", move),
                ("queued", "session.inbox.enqueued.1", {"item": {"type": "move", "payload": move}}),
                ("text", "session.inbox.enqueued.1", {"item": {"type": "user", "payload": {"text": old}}}),
            )
            for identifier, kind, value in events:
                connection.execute("INSERT INTO event VALUES (?, ?, ?)", (identifier, kind, json.dumps(value)))
            connection.execute("INSERT INTO session_inbox VALUES ('move', 'move', ?)", (json.dumps(move),))
            verbatim = json.dumps({"text": old, "location": location}, separators=(",", ":"))
            connection.execute("INSERT INTO session_message VALUES ('message', ?)", (verbatim,))
            connection.commit()
        finally:
            connection.close()
        original = source.read_bytes()
        self.copy("opencode")
        connection = sqlite3.connect(self.stage["data"] / "opencode/opencode.db")
        try:
            self.assertEqual(connection.execute("SELECT directory,path FROM session_v2").fetchone(), (new, "subdir"))
            worktree, sandboxes = connection.execute("SELECT worktree,sandboxes FROM project").fetchone()
            self.assertEqual(worktree, new)
            self.assertEqual(json.loads(sandboxes), [new, outside])
            for table in ("project_directory", "worktree"):
                self.assertEqual(connection.execute(f"SELECT directory FROM {table}").fetchone()[0], new)
            records = {identifier: json.loads(data) for identifier, data in connection.execute("SELECT id,data FROM event")}
            self.assertEqual(records["created"]["location"], {"directory": new, "workspaceID": "unchanged-workspace"})
            self.assertEqual(records["created"]["title"], old)
            self.assertEqual(records["moved"]["location"]["directory"], new)
            self.assertEqual(records["queued"]["item"]["payload"]["location"]["directory"], new)
            self.assertEqual(records["text"]["item"]["payload"]["text"], old)
            self.assertEqual(json.loads(connection.execute("SELECT payload FROM session_inbox").fetchone()[0])["location"]["directory"], new)
            self.assertEqual(connection.execute("SELECT data FROM session_message").fetchone()[0], verbatim)
        finally:
            connection.close()
        self.assertEqual(source.read_bytes(), original)

    def test_managed_defaults_reseed_but_user_edits_survive(self):
        cases = [('opencode', 'xdg/opencode', '.opencode-bwrap-cscs-models', 'opencode.json'),
                 ('omp', 'home/.omp/agent', '.omp-bwrap-cscs-models', 'models.yml'),
                 ('prime-agent', 'home/.prime/agent', '.prime-agent-bwrap-cscs-models', 'models.json')]
        for index, (kind, directory, marker, name) in enumerate(cases):
            for edited in (False, True):
                with self.subTest(kind=kind, edited=edited):
                    source = self.roots(f'managed-source-{index}-{edited}')
                    target = self.roots(f'managed-target-{index}-{edited}')
                    stage = self.roots(f'managed-stage-{index}-{edited}')
                    base = source['config'] / directory
                    self.put(base / name, 'retired default' if not edited else 'user provider')
                    self.put(base / (marker + '.sha256'), hashlib.sha256(b'retired default').hexdigest())
                    job = payload.plan_profiles(kind, source, target, self.host, self.project)[0]
                    self.copy(kind, job=job, stage=stage)
                    self.assertEqual((stage['config'] / directory / name).exists(), edited)
                    if edited:
                        self.assertEqual((stage['config'] / directory / name).read_text(), 'user provider')

    def test_legacy_prime_provider_regenerates_only_with_matching_hash(self):
        for edited in (False, True):
            source = self.roots('legacy-prime-' + str(edited))
            stage = self.roots('legacy-prime-stage-' + str(edited))
            base = source['config'] / 'home/.prime/agent'
            self.put(base / 'extensions/cscs-provider.ts', 'custom provider' if edited else 'managed provider')
            self.put(base / '.prime-agent-bwrap-cscs-models.sha256', hashlib.sha256(b'managed provider').hexdigest())
            job = payload.plan_profiles('prime-agent', source, self.destination, self.host, self.project,
                                        source_release='legacy')[0]
            self.copy('prime-agent', job=job, stage=stage)
            self.assertEqual((stage['config'] / 'home/.prime/agent/extensions/cscs-provider.ts').exists(), edited)

    def test_owned_skills_regenerate_user_links_and_edits_survive(self):
        skills = self.source['config'] / 'home/.omp/agent/skills'
        markers = skills / '.omp-bwrap-packaged-skill-links'
        self.put(markers / 'packaged', '/user-tools/old-image/skills/packaged\n')
        (skills / 'packaged').symlink_to('/user-tools/old-image/skills/packaged')
        self.put(markers / 'edited', '/user-tools/old-image/skills/edited\n')
        (skills / 'edited').symlink_to('/my/custom/skill')
        (skills / 'user-link').symlink_to('/my/user/skill')
        self.put(skills / 'user-directory/SKILL.md', 'custom skill')
        self.copy('omp')
        copied = self.stage['config'] / 'home/.omp/agent/skills'
        self.assertFalse((copied / 'packaged').is_symlink())
        self.assertEqual(os.readlink(copied / 'edited'), '/my/custom/skill')
        self.assertEqual(os.readlink(copied / 'user-link'), '/my/user/skill')
        self.assertEqual((copied / 'user-directory/SKILL.md').read_text(), 'custom skill')

    def test_legacy_skill_directories_regenerate_without_losing_user_files(self):
        skills = self.source['config'] / 'home/.omp/agent/skills'
        self.put(skills / 'managed/.omp-bwrap-packaged-skill', '')
        (skills / 'managed/SKILL.md').symlink_to('/user-tools/old-image/skills/managed/SKILL.md')
        self.put(skills / 'custom/.omp-bwrap-packaged-skill', '')
        self.put(skills / 'custom/SKILL.md', 'user replacement')
        self.copy('omp')
        copied = self.stage['config'] / 'home/.omp/agent/skills'
        self.assertFalse((copied / 'managed').exists())
        self.assertEqual((copied / 'custom/SKILL.md').read_text(), 'user replacement')

    def test_canonical_user_skill_marker_rebases_with_its_internal_link(self):
        skills = self.source['config'] / 'home/.omp/agent/skills'
        canonical = self.source['data'] / 'cscs/skills/custom'
        self.put(canonical / 'SKILL.md', 'user skill')
        self.put(skills / '.omp-bwrap-packaged-skill-links/custom', str(canonical) + '\n')
        (skills / 'custom').symlink_to(canonical)
        self.copy('omp')
        copied = self.stage['config'] / 'home/.omp/agent/skills'
        expected = str(self.destination['data'] / 'cscs/skills/custom')
        self.assertEqual(os.readlink(copied / 'custom'), expected)
        self.assertEqual((copied / '.omp-bwrap-packaged-skill-links/custom').read_text(), expected + '\n')

    def test_internal_links_rebase_and_external_dangling_links_are_preserved(self):
        self.put(self.source['data'] / 'bulk/item', 'history')
        (self.source['data'] / 'relative').symlink_to('bulk/item')
        (self.source['data'] / 'absolute').symlink_to(self.source['data'] / 'bulk')
        (self.source['data'] / 'external').symlink_to('/unavailable/custom-secret')
        self.copy('omp')
        self.assertEqual(os.readlink(self.stage['data'] / 'relative'), str(self.destination['data'] / 'bulk/item'))
        self.assertEqual(os.readlink(self.stage['data'] / 'absolute'), str(self.destination['data'] / 'bulk'))
        self.assertEqual(os.readlink(self.stage['data'] / 'external'), '/unavailable/custom-secret')

    def test_runtime_exclusions_keep_persistent_bulk(self):
        scenarios = [('opencode', 'state', 'opencode/locks/lock'),
                     ('omp', 'state', 'omp/run/pid'), ('omp', 'state', 'omp/ssh-control/socket'),
                     ('prime-agent', 'data', 'kernel-venv/bin/python'),
                     ('prime-agent', 'config', 'home/.prime/agent/daemon-workers/pid'),
                     ('prime-agent', 'state', 'daemons/log'),
                     ('paseo', 'data', 'paseo/runtime/plugin.js'), ('paseo', 'data', 'paseo/daemon.pid')]
        for index, (kind, key, relative) in enumerate(scenarios):
            source = self.roots(f'runtime-{index}')
            stage = self.roots(f'runtime-stage-{index}')
            self.put(source[key] / relative, 'runtime')
            self.put(source['data'] / 'user-bulk/data', 'durable')
            self.put(source['data'] / '.cscs-agent-storage/omp.lease/owner.json', {'pid': 5})
            if kind == 'paseo':
                self.put(source['config'] / 'harness-profile.json', {'mode': 'isolated'})
            job = payload.plan_profiles(kind, source, self.destination, self.host, self.project)[0]
            self.copy(kind, job=job, stage=stage)
            self.assertFalse((stage[key] / relative).exists())
            self.assertEqual((stage['data'] / 'user-bulk/data').read_text(), 'durable')
            self.assertFalse((stage['data'] / '.cscs-agent-storage/omp.lease').exists())

    def test_sqlite_is_opened_only_in_local_scratch_and_rewrites_owning_columns(self):
        source = self.database(self.source['data'] / 'opencode/opencode.db', self.source['data'] / 'workspace')
        before = source.read_bytes()
        real = payload.import_sqlite
        targets = []
        def observe(old, new):
            targets.append(new)
            self.assertTrue(new.is_relative_to(self.scratch))
            return real(old, new)
        with patch.object(payload, 'import_sqlite', side_effect=observe):
            self.copy('opencode')
        self.assertTrue(targets)
        with sqlite3.connect(self.stage['data'] / 'opencode/opencode.db') as db:
            self.assertEqual(db.execute('SELECT directory FROM session').fetchone()[0], str(self.destination['data'] / 'workspace'))
            self.assertEqual(db.execute('SELECT text FROM message').fetchone()[0], str(self.source['data']) + '/verbatim conversation')
        self.assertEqual(source.read_bytes(), before)

    def test_corrupt_database_fails_not_fresh(self):
        self.put(self.source['data'] / 'omp/agent.db', 'broken sqlite')
        with self.assertRaises((RuntimeError, sqlite3.DatabaseError)):
            self.copy('omp')

    def test_wal_database_snapshot_retains_uncheckpointed_rows_and_source_sidecars(self):
        database = self.database(self.source['data'] / 'omp/history.db')
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute("INSERT INTO message VALUES ('uncheckpointed durable row')")
        connection.commit()
        before = self.snapshot(self.source['data'])
        self.copy('omp')
        with sqlite3.connect(self.stage['data'] / 'omp/history.db') as copied:
            self.assertEqual(copied.execute('SELECT count(*) FROM message').fetchone()[0], 2)
        self.assertEqual(before, self.snapshot(self.source['data']))
        self.assertFalse((self.stage['data'] / 'omp/history.db-wal').exists())

    def test_unrelated_journal_files_are_not_silently_discarded(self):
        self.put(self.source['data'] / 'user-file', 'custom')
        self.put(self.source['data'] / 'user-file-journal', 'user history')
        self.copy('omp')
        self.assertEqual((self.stage['data'] / 'user-file-journal').read_text(), 'user history')

    def test_orphaned_database_wal_fails_instead_of_dropping_history(self):
        self.put(self.source['data'] / 'omp/history.db-wal', 'unrecoverable orphan')
        with self.assertRaisesRegex(RuntimeError, 'orphaned SQLite'):
            self.copy('omp')

    def test_each_checkpoint_kind_validates_inventory_after_rebasing(self):
        for kind in ('opencode', 'omp', 'paseo'):
            with self.subTest(kind=kind):
                self.source = self.roots('checkpoint-source-' + kind)
                self.stage = self.roots('checkpoint-stage-' + kind)
                if kind == 'paseo':
                    self.put(self.source['config'] / 'harness-profile.json', {'mode': 'isolated'})
                store, _ = self.checkpoint(kind)
                before = self.snapshot(self.source['data'])
                self.copy(kind)
                copied = self.stage['data'] / '.cscs-agent-storage' / payload.namespace(kind)
                generation = copied / 'generations' / (copied / 'current').read_text().strip()
                manifest = json.loads((generation / 'manifest.json').read_text())
                storage.validate_inventory(generation, manifest['files'])
                self.assertEqual(before, self.snapshot(self.source['data']))
                if kind == 'paseo':
                    config = json.loads((generation / 'paseo/config.json').read_text())
                    self.assertEqual(config['worktrees']['root'], str(self.destination['data'] / 'paseo/worktrees'))

    def test_invalid_current_uses_healthy_generation_without_source_repair(self):
        store, generation = self.checkpoint('opencode')
        self.put(store / 'current', 'broken\n')
        self.put(store / 'generations/broken/manifest.json', {'version': 2, 'kind': 'opencode', 'files': []})
        self.copy('opencode')
        target = self.stage['data'] / '.cscs-agent-storage/opencode-v2'
        self.assertEqual((target / 'current').read_text(), 'healthy\n')
        self.assertEqual((store / 'current').read_text(), 'broken\n')
        self.assertFalse((target / 'generations/broken').exists())

    def test_all_invalid_or_empty_checkpoint_current_fails_not_fresh(self):
        for name in ('bad-inventory', 'empty-current'):
            self.source = self.roots('invalid-' + name)
            self.stage = self.roots('invalid-stage-' + name)
            store, generation = self.checkpoint('opencode')
            if name == 'bad-inventory':
                (generation / 'opencode.db').write_text('corrupt')
            else:
                shutil.rmtree(generation)
                (store / 'current').write_text('')
            with self.assertRaisesRegex(RuntimeError, 'no valid'):
                self.copy('opencode')

    def test_legacy_v1_database_is_importer_input_not_a_v2_checkpoint(self):
        self.database(self.source['data'] / 'opencode/opencode-local.db')
        self.put(self.source['data'] / 'opencode/auth.json', {'key': 'secret'})
        job = self.job('opencode', source_release='legacy', source_namespace='opencode')
        self.copy('opencode', job=job)
        self.assertTrue((self.stage['data'] / 'opencode/opencode.db').is_file())
        self.assertFalse((self.stage['data'] / '.cscs-agent-storage/opencode-v2').exists())
        self.assertTrue((self.source['data'] / 'opencode/opencode-local.db').is_file())

    def test_legacy_v1_checkpoint_is_retained_but_not_relabelled_v2(self):
        store, _ = self.checkpoint('opencode', namespace='opencode')
        self.copy('opencode', job=self.job('opencode', source_release='26.08', source_namespace='opencode'))
        self.assertTrue((self.stage['data'] / '.cscs-migration-legacy/opencode/generations/healthy/opencode.db').is_file())
        self.assertTrue((self.stage['data'] / 'opencode/opencode.db').is_file())
        self.assertFalse((self.stage['data'] / '.cscs-agent-storage/opencode-v2').exists())
        self.assertEqual((store / 'current').read_text(), 'healthy\n')

    def test_legacy_omp_config_databases_are_locally_staged(self):
        self.database(self.source['config'] / 'home/.omp/agent/agent.db')
        self.session(self.source['config'] / 'home/.omp/agent/sessions/history.jsonl')
        self.copy('omp', job=self.job('omp', source_release='legacy'))
        self.assertTrue((self.stage['config'] / 'home/.omp/agent/agent.db').is_file())
        self.assertTrue((self.stage['config'] / 'home/.omp/agent/sessions/history.jsonl').is_file())

    def test_paseo_home_project_dependents_follow_skipped_release(self):
        for mode in ('home', 'project:retained'):
            self.put(self.source['config'] / 'harness-profile.json', {'mode': mode})
            jobs = payload.plan_profiles('paseo', self.source, self.destination, self.host,
                                         self.project, source_release='26.07')
            self.assertEqual([job['kind'] for job in jobs], ['paseo', 'opencode', 'omp'])
            for job in jobs[1:]:
                self.assertEqual(job['source_release'], '26.07')
                self.assertIn('26.07', str(job['source']['data']))
                self.assertIn('26.11', str(job['destination']['data']))

    def test_paseo_external_v1_owner_uses_original_namespace_and_importer(self):
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'home'})
        old = {key: self.host[key] / 'opencode-cscs-agents-26.08' for key in payload.ROOTS}
        self.database(old['data'] / 'opencode/opencode-local.db')
        jobs = payload.plan_profiles('paseo', self.source, self.destination, self.host,
                                     self.project, source_release='26.08')
        native = next(job for job in jobs if job['kind'] == 'opencode')
        self.assertEqual(native['source_namespace'], 'opencode')
        self.assertEqual(native['source'], old)
        self.assertIn('opencode-v2-cscs-agents-26.11', str(native['destination']['data']))
        self.copy('opencode', job=native, stage=self.roots('v1-dependent-stage'))

    def test_prime_native_headers_and_auxiliary_ledgers_are_distinguished(self):
        # Prime v0.10.0 pa-types/src/session.rs: FileEntry::Header.
        original = self.source['data'] / 'sessions/history.jsonl'
        header = {'type': 'session', 'version': 3, 'id': 'native-prime-session',
                  'timestamp': '2026-09-01T00:00:00.000Z', 'cwd': str(self.project),
                  'parentSession': str(self.source['data'] / 'sessions/parent.jsonl'),
                  'rlmDepth': 0, 'git': {'repoUrl': 'fixture', 'branch': 'main'}}
        conversation = json.dumps({'type': 'message', 'id': 'm', 'parentId': None,
            'timestamp': '2026-09-01T00:00:00.000Z',
            'message': {'role': 'user', 'content': str(self.source['data'])}})
        self.put(original, json.dumps(header) + '\n' + conversation + '\n')
        semantic = self.put(self.source['data'] / 'session-artifacts/native-prime-session/semantic-edges.jsonl',
                            '{"type":"session_registered","session_id":"native-prime-session"}\n{"type":"future_event"}')
        auxiliary = self.put(self.source['data'] / 'sessions/metadata/custom.jsonl',
                             '{"type":"custom_metadata","cwd":"' + str(self.source['data']) + '"}\n')
        self.copy('prime-agent')
        copied = self.stage['data'] / 'sessions/history.jsonl'
        first, rest = copied.read_text().split('\n', 1)
        self.assertEqual(json.loads(first)['cwd'], str(self.project))
        self.assertEqual(json.loads(first)['parentSession'], str(self.destination['data'] / 'sessions/parent.jsonl'))
        self.assertEqual(rest, conversation + '\n')
        self.assertEqual((self.stage['data'] / semantic.relative_to(self.source['data'])).read_bytes(), semantic.read_bytes())
        self.assertEqual((self.stage['data'] / auxiliary.relative_to(self.source['data'])).read_bytes(), auxiliary.read_bytes())

    def test_prime_rlm_topology_paths_and_ledger_hash_rebase_without_prompt_edits(self):
        agent = self.source['config'] / 'home/.prime/agent'
        parent = self.source['data'] / 'sessions/parent.jsonl'
        child_dir = agent / 'rlm-sessions/child'
        child = child_dir / 'sessions/child.jsonl'
        old_hash = hashlib.sha256(os.fsencode(parent.parent)).hexdigest()[:16]
        spawn = {'v': 1, 'op': 'spawn', 'at': 'fixture', 'childId': 'child', 'parent': str(parent),
                 'child': str(child), 'depth': 1, 'name': str(parent)}
        ledger = self.put(agent / f'rlm-ledger/{old_hash}.jsonl', json.dumps(spawn) + '\n')
        display = {'type': 'rlm_subagent', 'childId': 'child', 'sessionDir': str(child_dir),
                   'sessionFile': str(child), 'status': 'completed', 'prompt': str(parent),
                   'spawnCode': str(child)}
        self.put(child_dir / 'rlm-subagent.json', display)
        registry = self.put(self.source['data'] / 'session-artifacts/parent/rlm-subagents.jsonl',
                            json.dumps({'childId': 'child', 'sessionDir': str(child_dir),
                                        'sessionFile': str(child), 'parentSessionFile': str(parent),
                                        'prompt': str(parent), 'status': 'completed'}) + '\n')
        before = {key: self.snapshot(root) for key, root in self.source.items()}
        self.copy('prime-agent')
        new_parent = self.destination['data'] / 'sessions/parent.jsonl'
        new_hash = hashlib.sha256(os.fsencode(new_parent.parent)).hexdigest()[:16]
        copied_agent = self.stage['config'] / 'home/.prime/agent'
        copied_ledger = copied_agent / f'rlm-ledger/{new_hash}.jsonl'
        self.assertFalse((copied_agent / f'rlm-ledger/{old_hash}.jsonl').exists())
        record = json.loads(copied_ledger.read_text())
        self.assertEqual(record['parent'], str(new_parent))
        self.assertEqual(record['name'], str(parent))
        copied_display = json.loads((copied_agent / 'rlm-sessions/child/rlm-subagent.json').read_text())
        self.assertEqual(copied_display['sessionFile'], str(self.destination['config'] / child.relative_to(self.source['config'])))
        self.assertEqual(copied_display['prompt'], str(parent))
        copied_registry = json.loads((self.stage['data'] / registry.relative_to(self.source['data'])).read_text())
        self.assertEqual(copied_registry['parentSessionFile'], str(new_parent))
        self.assertEqual(copied_registry['prompt'], str(parent))
        self.assertEqual(before, {key: self.snapshot(root) for key, root in self.source.items()})

    def test_paseo_nested_native_profiles_sessions_and_leases(self):
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'isolated'})
        instance = 'a' * 32
        harness = self.source['data'] / 'harnesses'
        self.put(harness / 'opencode/default/config/user.json', {'custom': True})
        self.database(harness / 'opencode/default/data/opencode/opencode.db')
        self.put(harness / f'omp/{instance}/config/home/.omp/agent/auth.json', {'secret': 'token'})
        self.put(harness / f'omp/{instance}/state/omp/run/pid', 'runtime')
        session = self.session(harness / f'omp-sessions/{instance}/history.jsonl', self.source['data'] / 'paseo/worktrees/branch')
        body = {'id': 'agent', 'provider': 'omp', 'cwd': str(self.project), 'title': str(session),
                'persistence': {'provider': 'omp', 'sessionId': 'native', 'nativeHandle': str(session),
                                'metadata': {'cwd': str(self.project), 'title': str(session)}}}
        self.put(self.source['data'] / 'paseo/agents/project/agent.json', body)
        job = self.job('paseo')
        owners = payload.lease_profiles([job])
        self.assertEqual([owner['kind'] for owner in owners], ['paseo', 'omp', 'opencode'])
        before = self.snapshot(self.source['data'])
        self.copy('paseo', job=job)
        copied = json.loads((self.stage['data'] / 'paseo/agents/project/agent.json').read_text())
        self.assertEqual(copied['cwd'], str(self.project))
        self.assertEqual(copied['persistence']['nativeHandle'], str(self.destination['data'] / session.relative_to(self.source['data'])))
        self.assertEqual(copied['title'], str(session))
        self.assertEqual(copied['persistence']['metadata']['title'], str(session))
        copied_session = self.stage['data'] / session.relative_to(self.source['data'])
        first, conversation = copied_session.read_text().splitlines()
        self.assertEqual(json.loads(first)['cwd'], str(self.destination['data'] / 'paseo/worktrees/branch'))
        self.assertEqual(json.loads(conversation)['message']['content'], str(self.source['data']))
        self.assertFalse((self.stage['data'] / f'harnesses/omp/{instance}/state/omp/run').exists())
        self.assertEqual(before, self.snapshot(self.source['data']))

    def test_paseo_external_native_owner_is_copied_not_reused(self):
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'home'})
        jobs = payload.plan_profiles('paseo', self.source, self.destination, self.host, self.project)
        native = next(job for job in jobs if job['kind'] == 'omp')
        session = self.session(native['source']['data'] / 'omp/sessions/history.jsonl')
        self.put(self.source['data'] / 'paseo/agents/agent.json', {
            'id': 'agent', 'provider': 'omp', 'cwd': str(self.project),
            'persistence': {'provider': 'omp', 'sessionId': 'native', 'nativeHandle': str(session)}})
        mappings = [(owner['source'][key], owner['destination'][key]) for owner in payload.lease_profiles(jobs) for key in payload.ROOTS]
        self.copy('paseo', job=jobs[0], mappings=mappings)
        record = json.loads((self.stage['data'] / 'paseo/agents/agent.json').read_text())
        self.assertEqual(record['persistence']['nativeHandle'], str(native['destination']['data'] / 'omp/sessions/history.jsonl'))
        self.copy('omp', job=native, stage=self.roots('external-native-stage'), mappings=mappings)
        self.assertTrue(session.is_file())

    def test_paseo_registries_schedules_rebase_only_owning_paths(self):
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'isolated'})
        old = str(self.source['data'] / 'paseo/worktrees/branch')
        self.put(self.source['data'] / 'paseo/projects/projects.json', [{'projectId': 'p', 'rootPath': str(self.project), 'displayName': old}])
        self.put(self.source['data'] / 'paseo/projects/workspaces.json', [{'workspaceId': 'w', 'cwd': old, 'worktreeRoot': old, 'mainRepoRoot': str(self.project), 'title': old}])
        self.put(self.source['data'] / 'paseo/schedules/schedule.json', {'target': {'type': 'new-agent', 'config': {'cwd': old, 'systemPrompt': old}}, 'prompt': old})
        self.copy('paseo')
        workspaces = json.loads((self.stage['data'] / 'paseo/projects/workspaces.json').read_text())
        self.assertEqual(workspaces[0]['cwd'], str(self.destination['data'] / 'paseo/worktrees/branch'))
        self.assertEqual(workspaces[0]['mainRepoRoot'], str(self.project))
        self.assertEqual(workspaces[0]['title'], old)
        schedule = json.loads((self.stage['data'] / 'paseo/schedules/schedule.json').read_text())
        self.assertEqual(schedule['target']['config']['systemPrompt'], old)
        self.assertEqual(schedule['prompt'], old)

    def test_paseo_missing_or_unknown_owner_fails_instead_of_guessing(self):
        self.put(self.source['data'] / 'paseo/agents/record.json', {'id': 'history'})
        with self.assertRaisesRegex(RuntimeError, 'lacks harness-profile'):
            self.job('paseo')
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'ambiguous'})
        with self.assertRaisesRegex(RuntimeError, 'selection'):
            self.job('paseo')

    def test_paseo_linked_native_profile_fails_before_any_copy(self):
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'isolated'})
        outside = self.root / 'outside'
        outside.mkdir()
        harness = self.source['data'] / 'harnesses/omp'
        harness.mkdir(parents=True)
        (harness / ('b' * 32)).symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'isolated native'):
            self.job('paseo')
        self.assertFalse(self.stage['data'].exists())

    def test_paseo_unowned_session_handle_fails_without_touching_source(self):
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'isolated'})
        self.put(self.source['data'] / 'paseo/agents/record.json', {'id': 'a', 'provider': 'omp', 'cwd': str(self.project),
            'persistence': {'provider': 'omp', 'nativeHandle': '/external/unowned.jsonl', 'sessionId': 's'}})
        with self.assertRaisesRegex(RuntimeError, 'no copied owner'):
            self.copy('paseo')

    def git(self, *arguments, cwd=None):
        return subprocess.run(['git', *map(str, arguments)], cwd=cwd, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_OPTIONAL_LOCKS': '0'})

    def test_paseo_external_git_worktree_bundles_admin_without_source_changes(self):
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'isolated'})
        self.git('init', self.project)
        self.put(self.project / 'tracked.txt', 'initial\n')
        self.git('add', 'tracked.txt', cwd=self.project)
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture', cwd=self.project)
        worktree = self.source['data'] / 'paseo/worktrees/topic'
        worktree.parent.mkdir(parents=True)
        self.git('worktree', 'add', '-b', 'fixture-topic', worktree, cwd=self.project)
        self.put(worktree / 'tracked.txt', 'uncommitted user edit\n')
        self.put(worktree / 'untracked.txt', 'user file\n')
        before = self.snapshot(self.project / '.git')
        job = self.job('paseo')
        self.assertEqual(payload.external_metadata([job]), [self.project / '.git'])
        self.copy('paseo', job=job)
        for key in payload.ROOTS:
            self.destination[key].parent.mkdir(parents=True, exist_ok=True)
            self.stage[key].rename(self.destination[key])
        copied = self.destination['data'] / 'paseo/worktrees/topic'
        self.assertIn('.cscs-migration-git', (copied / '.git').read_text())
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', 'HEAD', cwd=copied).stdout.strip(), b'fixture-topic')
        self.assertEqual((copied / 'tracked.txt').read_text(), 'uncommitted user edit\n')
        self.assertEqual((copied / 'untracked.txt').read_text(), 'user file\n')
        self.assertEqual(before, self.snapshot(self.project / '.git'))
        self.assertIn('worktrees/topic', (worktree / '.git').read_text())

    def test_unsafe_git_alternates_fail_without_publication(self):
        self.put(self.source['config'] / 'harness-profile.json', {'mode': 'isolated'})
        self.git('init', self.project)
        self.put(self.project / 'tracked', 'data')
        self.git('add', 'tracked', cwd=self.project)
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture', cwd=self.project)
        tree = self.source['data'] / 'paseo/worktrees/topic'
        tree.parent.mkdir(parents=True)
        self.git('worktree', 'add', '-b', 'fixture-topic', tree, cwd=self.project)
        self.put(self.project / '.git/objects/info/alternates', '/external/objects\n')
        with self.assertRaisesRegex(RuntimeError, 'external object dependencies'):
            self.copy('paseo')
        self.assertFalse(self.destination['data'].exists())

    def test_omp_ancillary_jsonl_bytes_survive_without_session_header_assumption(self):
        ledger = self.put(self.source['data'] / 'omp/sessions/history/artifact-ledger.jsonl',
                          '{"type":"artifact","cwd":"' + str(self.source['data']) + '"}\n{"torn":')
        self.session(self.source['data'] / 'omp/sessions/project/history.jsonl')
        self.copy('omp')
        self.assertEqual((self.stage['data'] / ledger.relative_to(self.source['data'])).read_bytes(), ledger.read_bytes())

    def test_custom_provider_database_is_validated_but_not_reinterpreted(self):
        database = self.database(self.source['config'] / 'custom-provider/opencode.db', self.source['data'] / 'custom-project')
        self.copy('opencode')
        with sqlite3.connect(self.stage['config'] / 'custom-provider/opencode.db') as copied:
            self.assertEqual(copied.execute('SELECT directory FROM session').fetchone()[0], str(self.source['data'] / 'custom-project'))

    def test_overlapping_or_symlinked_roots_rejected(self):
        destination = dict(self.destination)
        destination['state'] = destination['data'] / 'nested'
        with self.assertRaisesRegex(RuntimeError, 'overlapping'):
            payload.plan_profiles('omp', self.source, destination, self.host, self.project)
        self.source['config'].parent.mkdir(parents=True)
        self.source['config'].symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'non-symlink'):
            self.job('omp')


if __name__ == '__main__':
    unittest.main()
