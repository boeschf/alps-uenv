# CSCS coding agents 26.11

This recipe provides **OpenCode 2.3.0**, **Oh-My-Pi (OMP) 18.22.0**, **Prime Agent 0.10.3**, and **Paseo 0.16.1**, with isolated launchers, production CSCS inference defaults, live approval-gated CSCS skill installation, ripgrep, ShellCheck, jq, and yq. Internal launcher, model-config, and local-skill packages are version `2026.11.20`.

**Release status:** source/package and ARM64 runtime fixtures have been exercised; no 26.11 Stackinator configuration or image build has been performed. Examples using the release name require an image built and registered by the user. See [BUILD_NOTES.md](BUILD_NOTES.md) for the user-only build handoff and remaining acceptance checks.

## Start a native harness

```bash
export CSCS_INFERENCE_API_KEY='your-production-key'
uenv run cscs-agents/26.11 -- omp-bwrap
uenv run cscs-agents/26.11 -- opencode-bwrap
uenv run cscs-agents/26.11 -- prime-agent-bwrap
```

`CSCS_API_KEY` remains a production-key compatibility alias. Key values are not written into generated model configuration. This release has **only production CSCS discovery**: no experimental provider, key, fallback, pricing query, cost fields, or price-based selection. Personal providers and authentication remain available.

With the `agents` view already active, invoke the wrappers directly. The current directory is the writable project; `--project-dir PATH` selects another exact directory, not a discovered repository root. Separate launcher and harness arguments with `--`:

```bash
omp-bwrap --project-dir /path/to/project -- --continue
opencode-bwrap --project-dir /path/to/project
prime-agent-bwrap --project-dir /path/to/project -- list
```

Use each wrapper's `--help` for bind/hide paths, GPU controls, SSH-agent forwarding, a sandbox shell, and explicit profile roots. A fresh OMP interactive profile can display its provider setup wizard; CSCS-only users can press Escape to skip unrelated provider login.

## Profiles, persistence, and simultaneous instances

Default host roots are `${XDG_CONFIG_HOME:-$HOME/.config}`, `${XDG_DATA_HOME:-$HOME/.local/share}`, `${XDG_CACHE_HOME:-$HOME/.cache}`, and `${XDG_STATE_HOME:-$HOME/.local/state}`, each with a release-specific stem:

| Harness | Stem | Configuration below its config root |
|---|---|---|
| OpenCode | `opencode-v2-cscs-agents-26.11` | `xdg/opencode` |
| OMP | `omp-cscs-agents-26.11` | `home/.omp/agent` |
| Prime Agent | `prime-agent-cscs-agents-26.11` | `home/.prime/agent` |
| Paseo | `paseo-cscs-agents-26.11` | launcher-managed home/XDG layout |

These formulas use the **host's actual XDG environment**. Sites or shell setup may use architecture-qualified roots such as `$HOME/.local/aarch64/share`; do not substitute an unqualified `$HOME/.local/share` when finding existing data. ARM64 and x86-64 binaries/images are distinct. Shared persistent profiles are single-writer even across architectures/nodes; mutable tools and runtime databases are node-local, not portable running processes.

Project-local profiles are opt-in and never import home profiles. A new instance starts fresh; an older release of the same project-local instance can be copied with the migration workflow below:

```bash
omp-bwrap --project-local --instance implementer
omp-bwrap --project-local --instance reviewer
```

The path is `.cscs-agents/26.11/{opencode-v2,omp,prime-agent,paseo}/INSTANCE/{config,data,cache,state}` below the selected project. The instance defaults to `default`; names start with an ASCII letter/digit and otherwise contain only letters, digits, `.`, `_`, or `-`. The options work for all four wrappers. Project-local mode cannot be combined with explicit `--config-dir`, `--data-dir`, `--cache-dir`, or `--state-dir`.

Distinct instances can run concurrently. A duplicate OpenCode/OMP writer is refused; Prime clients can reconnect to their profile's daemon. Profiles do not isolate source edits or Git operations: use worktrees for independent edits. New `.cscs-agents` directories receive an excluding `.gitignore`; profiles contain transcripts and possibly credentials and must not be committed. Symlinked project-profile directories are rejected.

### NFS-safe storage and recovery

SQLite/WAL databases and lock-sensitive state must not run directly on the CSCS NFS home filesystem. The wrappers use validated node-local storage (default `/dev/shm/cscs-agent-storage-$(id -u)`) and distributed persistent writer leases. OpenCode databases and OMP non-bulk data/state are restored locally and published as validated immutable checkpoint generations. Large OMP sessions, blobs, plugins, and worktrees remain directly persistent. Prime sessions/kernel environment remain persistent, while PID ownership and discovery state are local. Paseo's identity/configuration are checkpointed; its agent/project/schedule/plugin-setting/worktree bulk directories remain persistent.

OpenCode and OMP checkpoint every 30 seconds and on clean exit (`CSCS_AGENT_CHECKPOINT_SECONDS` overrides the interval). An abrupt failure can lose changes since the **last successful** checkpoint; failed checkpoints retain the preceding complete generation. Restoration tries healthy older generations if the current one is invalid. Cache, runtime locks, and transient process identities are not restored as live processes.

`CSCS_AGENT_LOCAL_STORAGE_DIR` can select another owned, non-symlink local directory. Network-backed storage and overlapping extra binds are rejected. A lease is never stolen automatically. First establish that its process/allocation is gone, then use the **same profile flags** with `--recover-storage`; recovery quarantines rather than deletes the old lease.

## Existing history: first-launch copy migration

On the first launch into empty 26.11 roots, each wrapper searches **stored profiles**, not installed images or executables. It offers to copy compatible older data after showing the source and destination paths. **No previous profile, or only old cache files: start fresh without a prompt.** You can skip releases; discovery considers numerically older versioned roots and known unversioned `opencode-spack`, `omp-spack`, and `prime-agent-spack` home profiles. Newer releases are never imported backwards.

Home launches search the host's actual XDG roots. Project-local launches search only the same project, harness, and instance across older releases; they never select home history or another instance. Architecture-qualified XDG roots remain separate unless you deliberately select a shared environment.

Stop old native writers and Paseo daemons before accepting. Migration takes coordination leases, copies into private staging directories, and publishes only into empty destinations. It preserves the original persistent files: **no additional backup is made**, and the old profile remains the rollback copy. Do not run old and new writers against the same source while copying. Active or unresolved stale leases are refused, not stolen.

Legacy launchers without the distributed lease protocol cannot advertise an active writer to the new launcher; stopping them is a manual prerequisite. Empty pre-created 26.11 directories are accepted. If a preliminary launch already wrote defaults or other files, review that profile and move it aside or deliberately discard it before migration; the wrapper will not decide that those files are disposable.

Interactive launch asks before copying; declining starts fresh. Noninteractive launch with a candidate requires an explicit choice:

```bash
# Approval to copy the discovered older profile, then resume using native options:
omp-bwrap --migrate-profile -- --continue

# Several stored versions: select one explicitly, including across skipped releases.
omp-bwrap --migrate-from 26.10 --migrate-profile -- --continue

# Deliberately start a separate profile without importing older data:
omp-bwrap --no-migrate-profile

# Same project-local instance from a previous release:
omp-bwrap --project-local --instance reviewer --migrate-profile
```

If several candidates exist, the interactive workflow requires a selection; headless use requires `--migrate-from`. A selector is a version or `legacy`; OpenCode can need `26.10:opencode-v2` or `26.10:opencode` to distinguish v2 and v1 layouts. **Selecting a source is not consent**: without `--migrate-profile`, copying still requires interactive approval. An explicitly requested missing source is an error, not a silent fresh start. `--migrate-profile` without any candidate safely starts fresh.

Copies include portable settings, authentication, sessions, checkpoint data, and associated persistent bulk history. SQLite copies are opened only in validated node-local scratch space. OpenCode v1 data is staged for the native v2 importer, not relabelled as a v2 checkpoint. Unmodified hash-owned launcher defaults and packaged-skill links are regenerated; user modifications are retained. Cache, sockets, PIDs, leases, generated runtime/plugin/context files, and Prime's path-/architecture-dependent kernel environment are not carried over as live state.

Paseo retains its stored isolated/home/project native-history owner. Migration includes the corresponding native profiles rather than pointing the new daemon at the old rollback profile. Existing dependent destinations are not merged. Supported linked Git worktrees receive private copied Git metadata; external metadata paths are included in the approval preview, and unsafe layouts fail before publication. Historical conversation text and unrelated project paths are not rewritten.

An already initialized destination is never merged or overwritten. Ordinary launches use it as-is; explicit copying into it is refused. Interrupted publication leaves a durable marker outside the destination roots, and later launches fail closed with the affected paths rather than treating a partial copy as complete. Review the reported staged/published paths and establish that no migration process remains before manual recovery; the original source remains available.

Explicit `--config-dir`, `--data-dir`, `--cache-dir`, or `--state-dir` launches keep their existing **reuse these roots** meaning and bypass automatic discovery. Explicit migration into arbitrary roots is refused instead of guessing a source. Reusing old roots lets the new harness update them and therefore does **not** preserve a rollback copy. Do not run old/new writers together or downgrade-write upgraded data.

Sessions retain their project association; use the harness's all-projects listing if the current project appears empty. A newer timestamp or an integrity-valid empty checkpoint does not prove that it contains the wanted history. Verify the selected source's sessions rather than choosing project history when you intended the home profile.

### Upgrade compatibility

- OMP 18.14 requires exact provider-qualified model IDs. 18.18.3 adds HTML ask forms; 18.19 removes `taskFile` and blocked-todo support. 18.20 secret patterns use Rust regex (no PCRE lookaround/backreferences); old `$$NAME_HASH:CASE$$` placeholders remain literal, so start a new session when those occur. 18.22 renames `ida.*` tools to `disassembler.*`. Review custom prompts/tools against the [OMP changelog](https://github.com/can1357/oh-my-pi/blob/v18.22.0/packages/coding-agent/CHANGELOG.md).
- OpenCode 2.2 changes SDK/`@opencode/ai` Promise imports and OpenTUI 0.7 plugin interfaces and removes its GitHub Action. 2.3 enables background subagents by default and lowers default TUI verbosity. Review integrations rather than copying v1 plugins. Use `AGENTS.md`; no language servers are configured by this image.
- Prime 0.10.1 removes ignored tool-selection flags; 0.10.3 removes Python bash `yield_after`. Update scripts/custom tools accordingly.

## Production models and refresh ownership

Production discovery reads `https://api.inference.cscs.ch/v1/models`, CSCS documentation, and optionally public Hugging Face metadata. It does metadata GETs, not inference or price requests. The observed catalogue contained nine production IDs; seven are exposed to these coding agents:

- `swiss-ai/Apertus-v1.5-70B`, `swiss-ai/Apertus-v1.5-8B`;
- `google/gemma-4-31B-it`;
- `zai-org/GLM-5.3-Flash`, `zai-org/GLM-5.3`;
- `Qwen/Qwen3.8-27B-FP8`, `Qwen/Qwen3.8-Flash-Next-FP8`.

The two thinking Apertus variants are excluded because tool use is disabled. Normal Apertus is manually selectable only: it is **never** a default, helper/title model, or automatic fallback. Roles are `default = cscs/zai-org/GLM-5.3-Flash`, `smol = cscs/Qwen/Qwen3.8-27B-FP8`, and `slow = cscs/zai-org/GLM-5.3`. OpenCode and Prime default to GLM-5.3-Flash as well. GLM contexts are 1,048,576 tokens; the other current models use 262,144. Output limits are capped at half the context rather than trusting gateway `maxTokens`.

Static production defaults allow startup before refresh. With a production key, launch-driven refresh is due on credential availability changes or after 24 hours. It replaces a complete managed file atomically; failure retains the old file/stamp and retries on a later launch. `CSCS_MODEL_REFRESH_VERBOSE=1` exposes errors. No background pricing or experimental discovery occurs.

SHA-256 ownership sidecars protect user edits: changing a managed file makes it user-owned and stops automatic overwrites. OpenCode manages `opencode.json`, not user `opencode.jsonc` (both merge). OMP user `models.yaml` takes precedence over managed `models.yml`. Prime manages native `models.json` and seeds `settings.json`; edits are respected. Use the local `refresh-cscs-models` skill to preview a refresh rather than overwriting customized files. OpenCode/OMP use the native `/v1` base; Prime uses the origin because it appends `/v1/messages` itself.

## Skills: live selection, durable installation

No upstream CSCS skill bundle is vendored. Three local skills ship with this release:

- **`manage-cscs-skills`** discovers the live [CSCS agent documentation](https://docs.cscs.ch/agents/) and its `.well-known/agent-skills/index.json`, presents available skills and installation/update choices, and asks before changes.
- **`refresh-cscs-models`** explains production-only model regeneration and ownership-safe refreshes.
- **`cscs-agent-sandbox`** explains filesystem/uenv/container restrictions and requires explicit consent for execution outside the sandbox through Slurm.

Ask an agent to discover current CSCS skills and propose a selection. Before installation or update it must show the selected skills, destination paths, exact commands, and archive digests and obtain approval. Cancellation or an unavailable headless question tool means **ask in final plain text and stop**, not implied consent. No startup network skill update occurs. Installed skills remain usable offline.

The canonical durable host root is exactly:

```bash
${XDG_DATA_HOME:-$HOME/.local/share}/cscs/skills
```

`CSCS_AGENT_SKILLS_DIR` is the explicit absolute override and must end in `/cscs/skills`. Export it **before** launching native harnesses or Paseo to select one shared root across architectures if desired. Otherwise architecture-qualified host XDG data roots give architecture-qualified skill roots. Installation verifies archive hashes, stages safe extraction, records `.digest`, protects conflicts, and previews updates/local-edit backups. Safe extraction needs a modern managed Python, not an arbitrary project Python:

```bash
env UV_PYTHON_PREFERENCE=only-managed uv run --no-project --python 3.13 python --version
```

Only the canonical subtree is bound writable at its host path and at sandbox `$XDG_DATA_HOME/cscs/skills`; the rest of OMP XDG data remains node-local. Discovery links are created in OpenCode `$XDG_CONFIG_HOME/opencode/skills`, OMP `$HOME/.omp/agent/skills`, and Prime `$PRIME_AGENT_CODING_AGENT_DIR/skills`. Per-link ownership markers repair managed links after image changes; user-owned directories/symlinks, including dangling links, are preserved.

**Paseo caveat:** workspace tools may inherit the daemon's HOME/XDG rather than the native helper's HOME/XDG. Use exported `CSCS_AGENT_SKILLS_DIR` as the reliable canonical destination, not a guessed `$HOME/.omp` inside a workspace command. The next native initialization synchronizes canonical skills into its automatic discovery links.

## Paseo: native workers, tmux, and SSH

`paseo-bwrap` runs a foreground daemon with its bundled web UI and native OpenCode/OMP adapters. Prime has no packaged Paseo adapter. One UI can connect to explicit daemons on ARM64 and x86-64 execution hosts; there is no automatic host discovery, Hub enrollment, remote startup, allocation, or live process migration.

Start tmux **first**, then run the daemon inside its pane on the intended execution host:

```bash
tmux new -s paseo-arm
export CSCS_INFERENCE_API_KEY='your-production-key'
export PASEO_PASSWORD='a-private-daemon-password'
uenv run cscs-agents/26.11 -- paseo-bwrap \
  --project-dir /path/to/project --project-local --instance arm \
  --listen 127.0.0.1:6767
```

Detach normally; reattach with `tmux attach -t paseo-arm`. Ctrl-C drains workers and checkpoints. Use another profile/port for a second daemon. For compute work, obtain a fresh allocation outside the agent sandbox, run on its compute node, and load the matching image there. tmux does not extend allocation wall time; login-node workload policy still applies.

From a workstation, tunnel to the **same fixed login host** where a login daemon runs (not a load-balanced alias that may choose another host):

```bash
ssh -N -L 17677:127.0.0.1:6767 user@fixed-login-host
# Open http://127.0.0.1:17677 on the workstation.
```

A compute daemon needs forwarding terminating on the compute node, subject to site policy; login-loopback forwarding alone cannot reach it. Add other daemons explicitly in the UI's Add host interface. Browser HTTP/WebSocket connections cannot initiate SSH. Desktop/CLI SSH connections require an already-running remote daemon. HTTPS proxies must forward WebSocket upgrades and configure allowed hosts/origins. Do not expose cleartext public endpoints. A password is required even on shared-node loopback; it grants the daemon owner's authority, not a multi-user isolation boundary. Relay, voice, and dictation are disabled. `PASEO_WEB_UI_ENABLED=false` disables UI serving.

### Isolated versus existing native history

Default `--harness-profile isolated` keeps native history inside the Paseo profile: OpenCode uses one helper for concurrent sessions; each new OMP session has its own writer profile, identified persistently by its JSONL path on resume. For existing **26.11 home** history, use a separate Paseo instance:

```bash
paseo-bwrap --project-dir /path/to/project --project-local \
  --instance existing-home --harness-profile home
```

For a named native project instance, use `--harness-profile project:reviewer`. Stop existing native writers first. Import/resume existing native sessions through Paseo; they are not automatically new Paseo agent records. These flags do not mean “find 26.10 history”: access/migrate old roots deliberately as described above. A Paseo profile fixes its history-owner selection and refuses silent switches; use another instance for another selection. Reuse the same flags after restart.

Select OMP **Ask** or **Write** explicitly when approval is wanted: upstream Full maps to `yolo`. OpenCode native sessions use its `build` mode, not a nonexistent generic `default` mode. The Paseo package corrects OMP's current tool-approval mapping: approval chooses only the current invocation, never a directory/session grant; denial remains denial. Real CLI-mediated approval and denial were exercised. Generic questions remain forms requiring answers, not blanket `permit allow` calls. Actual browser/UI acceptance remains a user check.

Paseo identity/configuration (`config.json`, keypair, server ID) is checkpointed; PID locks, local credentials, logs, generated plugins, helper directories, and model caches are disposable. Configure in Paseo or stop it and edit `$PASEO_HOME/config.json` through the same `paseo-bwrap ... --shell` profile, then exit to checkpoint. Provider routing/session discovery/worktree access and relay policy are launcher-managed.

## Prime service, uv, other uenvs, and containers

Prime's persistent supervisor/worker runs in a guard-owned sandbox and holds the writer lease. Discovery/socket identity includes node boot, allocation, project, profile, and isolation options. `list`, `status`, `doctor`, and `shutdown` must use the same wrapper/profile options; management queries do not start an absent daemon:

```bash
prime-agent-bwrap --project-local --instance reviewer -- status
prime-agent-bwrap --project-local --instance reviewer -- shutdown --force
```

uv and private managed Python bootstrap on each node under local `tools`, with a sibling local `uv-cache`. **Both** installation and cache must be local: uv's initial locks can hang on NFS before its timeout. Host curl/network access is needed on cold bootstrap. `CSCS_AGENT_TOOLS_DIR` and `CSCS_AGENT_TOOLS_CACHE_DIR` allow owned, non-symlink local overrides; network filesystems and masking extra binds are rejected. These tools/caches and `uv tool` installs are not checkpointed; session/configuration data is persistent. The private model-helper Python is not on project PATH. Prime creates a separate persistent kernel environment; `prime-rl` is not installed or required.

Another uenv must have its view active, not merely its image mounted:

```bash
uenv run cscs-agents/26.11,pytorch --view=agents,default -- omp-bwrap
uenv status
```

Use registered fully qualified names or actual image paths as appropriate. Project `python`/`python3` follow inherited PATH; uv project isolation does not automatically inherit PyTorch uenv packages.

`--podman` opts into host podman with packaged crun/catatonit/fuse-overlayfs, isolated user/mount namespaces, and RAM-backed layers/runtime under `/dev/shm`. It requires host podman, `/dev/fuse`, and GPU-device exposure (do not combine with `--no-gpu-devices`). RAM use counts against allocation memory. This path has not been container-execution tested for 26.11; do not treat package availability as a working-container claim.

## Sandbox and terminal behavior

The host root is read-only; normal home directories and `/tmp` are hidden. Only selected project/profile roots and explicit writable binds are writable. Network/GPU access is available by default; SSH-agent forwarding requires `--ssh-agent`. PID/IPC namespaces are private by default. This is the user's agent authority, not protection against a malicious user sharing credentials. OpenCode uses a foreground-owned private server; its launcher-managed read-only `service.json` disables a detached service and inherited `OPENCODE_CONFIG_DIR` is cleared.

The storage supervisor now forwards `SIGWINCH` to its child process group, independently of termination signals. Real ARM64 OMP/OpenCode TUI reflow was observed at 80×24 → 120×40; actual tmux-pane resize remains a post-build check. OMP's ED3 scrollback filter remains enabled (opt out with `OMP_BWRAP_FILTER_ED3=0`).

Nested Stackinator/bubblewrap builds cannot be assumed to work under the agent's no-new-privileges sandbox. Build outside it. **Any Slurm command that executes outside the client sandbox needs explicit permission first**: show target/allocation/account/resources, environment allowlist, CWD, paths/access, and exact command. `--uenv-passthrough=ignore` only ignores an already loaded uenv; it is not itself an escape mechanism. A Slurm task outside the client namespace has normal host filesystem authority. A forbidden stack build is not permission to move it into Slurm. See the local sandbox skill and [uenv Slurm integration](https://docs.cscs.ch/software/uenv/using/#slurm-integration).
