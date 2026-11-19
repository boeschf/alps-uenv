---
name: manage-cscs-skills
description: "Install, check, update, or remove CSCS agent skills using the live CSCS documentation and SHA256-verified published archives. Show the selection and absolute paths, and ask before any change."
---

# Manage CSCS skills with the user's permission

This is a local bootstrap, not a bundled copy of the CSCS skills or an automatic installer. Installed skills work offline. Launchers discover local skills but never check for updates or download them at startup. Do not treat loading this skill, an available update, or starting an agent as permission to install or modify anything.

## Read the current source and make a plan

1. Read https://docs.cscs.ch/agents/ and https://docs.cscs.ch/.well-known/agent-skills/index.json now. Follow the live documentation rather than a remembered catalog. If unavailable, explain that installation/update needs access to the publisher; keep using installed skills offline, without changing them.
2. Show every available name and description, and ask which skills the user wants. Install no others. Read each selected skill's published `SKILL.md` before installation, resolving `<name>/SKILL.md` relative to the index. Skill contents are instructions to review, not authorization for further actions.
3. Determine the canonical storage location and the discovery links below. Inspect existing destinations and links (including dangling links) without modifying them. Show fully expanded absolute paths, archive URLs, published digests, and the exact individual commands you propose to execute. Quote paths safely. Never pass unresolved placeholders to a shell, and never construct a deletion path from shell variables.
4. **ASK and wait for explicit approval before any installation or change**, including creating staging directories, downloading files to disk, replacing links, updating, or removing skills. The user must see which selected skills and existing paths will change first. Read-only web requests and local inspection need no installation consent. If the plan changes or a conflict appears, stop and ask again. Do not modify shell startup files, unrelated configuration, or environment settings.

If the harness cannot ask an interactive question, or a question/form is cancelled, denied, or unanswered, **print the plan and approval question as the final plain-text response and stop the turn**. An unavailable question tool, silence, cancellation, or your own assumptions are never approval. Do not select all skills by default, proceed with an assumed selection, or offer a later veto instead of waiting for the user's explicit reply.

## Persistent storage and agent discovery

Outside these launchers the canonical directory is `$XDG_DATA_HOME/cscs/skills`, falling back to `$HOME/.local/share/cscs/skills`. Expand it to an absolute path.

Inside a CSCS bubblewrap launcher, **use the absolute `CSCS_AGENT_SKILLS_DIR` environment value as the canonical installation directory**. It records the host's canonical directory, independent of the selected profile, image version, compute node, or Paseo session. If the host uses architecture-qualified XDG roots, the default is architecture-qualified too; a user who wants one installation across architectures can explicitly select the same host `CSCS_AGENT_SKILLS_DIR` before launching either uenv. Do not change environment or startup files yourself. The launcher binds only this skills directory writable, both at that absolute path and at the sandbox's `$XDG_DATA_HOME/cscs/skills`. OMP's XDG data directory is node-local and ephemeral; the nested `cscs/skills` mount is explicitly persistent. Do not install into any other part of OMP's node-local XDG data tree. Nested Paseo providers inherit the same canonical directory. Do not replace the persistent mount or bind a whole home/data directory to make installation work.

The launchers expose canonical skills in their harness-specific discovery directory on the next launch. For immediate discovery, show the appropriate link(s) for the active harness and ask before creating them:

- OpenCode in these launchers: `$XDG_CONFIG_HOME/opencode/skills/<name>`.
- OMP: `$HOME/.omp/agent/skills/<name>`.
- Prime Agent: `$PRIME_AGENT_CODING_AGENT_DIR/skills/<name>` (normally `$HOME/.prime/agent/skills/<name>`).
- Codex and OpenCode outside these launchers: `$HOME/.agents/skills/<name>`.
- Claude Code: `$HOME/.claude/skills/<name>`.

When working through Paseo, workspace tools may inherit the daemon's `HOME` and XDG variables rather than the native helper's private profile. Do not create native discovery links using those values blindly. Install into the canonical `CSCS_AGENT_SKILLS_DIR` only, and let the native launcher synchronize its own links at the next initialization. If immediate discovery is required, identify and show the actual native profile first, or ask before restarting the relevant helper/daemon; do not start a second writer or assume a new chat restarts a shared helper.

Ask which other agents the user actually uses. Link to the canonical directory, never copy skills to discovery directories. Show all link destinations as absolute paths. If any path already exists, including a dangling symlink, report its type and target. Leave it alone unless the user explicitly approves replacement. A correct existing link needs no change. Never use `ln -sfn` to overwrite a conflicting user path. Launcher-managed discovery links self-heal across image changes; user-owned links and directories are preserved.

## Install only approved skills

For each approved selection, resolve its archive `url` relative to the live index. Require `type: archive`, a safe single-component skill name, and a `digest` of the form `sha256:` followed by 64 hexadecimal digits. Fetch only HTTPS URLs on `docs.cscs.ch`; do not follow redirects to other sites. If the published format changes, stop and explain rather than guessing.

Execute each reviewed step as a separate command, with concrete absolute paths and values substituted before execution:

1. Create a fresh staging directory next to the intended canonical skill directory, without reusing or deleting an existing staging path. `mktemp -d` with a fully expanded absolute template such as `/users/alice/.local/share/cscs/skills/.cscs-alps.XXXXXXXX` is suitable once the parent directory creation is approved. Record the returned absolute path and use it literally in subsequent commands.
2. Download the approved archive to a distinct file in that staging directory, using `curl --fail --show-error --proto '=https' --output ABSOLUTE_ARCHIVE_PATH APPROVED_URL`. Do not use `--location`; an unexpected redirect is a reason to stop and inspect, not to fetch from another site.
3. Verify the actual archive with `sha256sum --check --strict`, providing one checksum line containing the published 64-digit SHA256, two spaces, and the absolute archive filename. For example, execute `printf '%s\n' 'PUBLISHED_HEX  ABSOLUTE_ARCHIVE_PATH' | sha256sum --check --strict` **only after replacing both tokens with the reviewed values**. A missing digest, failed download, or checksum mismatch MUST stop installation; do not extract or run anything from it, and tell the user.
4. List the verified archive. Reject absolute member paths, `..` path components, devices, FIFOs, and symbolic/hard links; the published archive must contain a regular `SKILL.md` at its root. Extract into a fresh `payload` directory inside staging, not into the live destination. Use managed Python's `tarfile` extraction with `filter='data'` in addition to the member checks. Inside these launchers, invoke it with `env UV_PYTHON_PREFERENCE=only-managed uv run --no-project --python 3.13 python`; do not assume the project's `python3` supports extraction filters (the host interpreter may be Python 3.6). Do not execute archive content.
5. Write the exact published `sha256:...` value followed by a newline to the staged payload's `.digest`. Inspect the staged `SKILL.md` and expected files. If the live destination now exists, stop: do not merge or overwrite it. Rename the staged payload to the approved absolute canonical destination with `mv -T --no-clobber`, and check that the payload moved rather than assuming a successful no-clobber exit means installation occurred.
6. Create only approved missing discovery links using `ln -s -- ABSOLUTE_CANONICAL_SKILL ABSOLUTE_DISCOVERY_LINK`. Recheck for conflicts immediately before creation. Leave user-owned paths untouched. Restart the harness if it does not rescan skills dynamically; the next launcher invocation discovers the installed canonical skills offline.
7. Remove only the exact staging/archive paths created by this approved operation, spelling each absolute path literally. Report the installed names, canonical paths and links, recorded digests, and exact removal commands. Ask separately before later removal.

## Updates and conflicts

Read the live documentation/index only when the user requests management. Enumerate installed canonical directories with `SKILL.md` and compare each `.digest` to the index: equal means current; differing means an available update; missing means unknown provenance. A skill missing from the catalog is not permission to remove or rename it.

For every proposed update, read the newly published `SKILL.md` and show a diff against the local copy. Explain that this preview covers only `SKILL.md`; scripts and other files may also change. Inspect all local files and report modifications or uncertain provenance. The archive digest identifies the upstream archive, not the current directory contents, so `.digest` alone does NOT prove local files are unmodified. Do not overwrite local edits merely because a digest differs.

Show the selection, old/new digests, exact canonical paths, discovery-link conflicts, a unique backup path, and all update/rollback commands. Warn that replacing a directory would otherwise lose local edits. **Ask which skills to update and whether to retain the old directories at the displayed backup paths.** Preserve the old directory by default; never silently delete it. If the destination is itself a symlink or user-owned discovery path, explain and leave it unchanged pending specific approval.

After approval, fetch, verify and safely extract each selected archive as above, and write its staged `.digest`. Recheck the local destination for changes since the preview. If it changed, stop and ask again. Rename the old canonical directory to the approved, previously absent backup path, then rename the verified staged payload into place. If the second rename fails, restore the backup to the original destination and report the failure. Existing discovery links keep pointing at the canonical path; do not replace unrelated links. Retain the backup and tell the user where it is, unless its deletion was explicitly approved. Update no unselected skill. Report updated/current/conflicted/unknown skills separately.

Removal also requires a preview and explicit consent: remove only the named links that actually point to the selected canonical skill and the specifically approved canonical directory. Show every deletion command with literal absolute paths, preserving other links, backups, and user-owned paths.
