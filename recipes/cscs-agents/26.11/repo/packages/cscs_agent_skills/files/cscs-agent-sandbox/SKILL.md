---
name: cscs-agent-sandbox
description: "Understand CSCS bubblewrapped agent filesystem, uenv, container, and Slurm boundaries. Use for nested uenv or Stackinator failures, podman, running work on compute nodes, and any proposed operation outside the agent sandbox. Require explicit approval before a Slurm escape."
---

# CSCS agent sandbox and uenv workflow

These instructions describe the CSCS agent launchers, not permission to escape them. A task request, loaded skill, available allocation, or failed command is not authorization to launch work outside the sandbox. If the user forbids a stack build or job submission, respect that restriction: prepare the recipe/commands and hand them off; do not submit a diagnostic job or try another route.

## Identify the current boundary without changing it

Use read-only checks such as `hostname`, `uname -m`, `uenv status`, the current `SLURM_JOB_ID`, and `srun --help`. Read the current [uenv documentation](https://docs.cscs.ch/software/uenv/using/#slurm-integration) and, if installed, the CSCS cluster-probes skill for account, partition and allocation facts. Do not submit work to discover those facts.

The launcher makes the host filesystem read-only, hides normal homes and `/tmp`, and binds the project and selected harness state writable. Its `HOME` is a private harness home, not necessarily the login home from the account database. Runtime SQLite, locks, sockets, and uv/Python tools are node-local; persistent profiles and checkpoints must retain their leases. Never make the whole host home or root writable, follow `/proc/.../root`, steal a lease, or change storage policy to bypass an error.

Inside Paseo, workspace tools can inherit the daemon's environment instead of a native helper's private `HOME`/XDG roots. Do not infer a native profile's ownership or configuration from those variables alone. Keep existing native profile/session routing; do not start a second writer to inspect it.

## Another uenv or a Stackinator build

A running uenv cannot mount another image inside the same process tree: the mount helper's no-new-privileges boundary prevents regaining setuid mount privileges. Bubblewrap adds another namespace boundary; nested Stackinator setup/build is not a supported local workflow.

- When a project only needs another environment's tools, the preferred solution is for the user to start a fresh host shell with the required images and views together, for example `uenv run cscs-agents/26.11,pytorch --view=agents,default -- omp-bwrap`. Resolve actual image labels and distinct mount points first. Mounting an image without activating its view does not provide its tools on `PATH`.
- For Stackinator, prepare and review recipe edits here, then hand configuration/build to the user outside the agent/uenv sandbox. Do not build a stack merely to test this skill.
- If the user instead wants a Slurm-launched operation outside the sandbox, use the approval process below. Do not silently substitute Slurm, SSH, another namespace, or a container for the requested execution location.

## Containers inside the existing sandbox

`--podman` is an opt-in launcher option: ask the user to relaunch the same profile with it if needed. It configures host podman with a dedicated user/mount namespace, local image/runtime storage, and the packaged crun/fuse-overlayfs tools. Container root maps to the current user; it does not grant host root. `/dev/fuse` must be available; do not combine with `--no-gpu-devices`.

Container images, volatile runtime state and layers consume node-local `/dev/shm` memory and are not a durable image registry. Project/profile permissions still apply. Do not start a Docker daemon, bypass the wrapper with privileged podman flags, or use containers as an unapproved escape. For scheduled application workloads, follow the current CSCS Container Engine/EDF guidance rather than assuming podman is the cluster execution service.

## Slurm escape: propose, ask, then execute only what was approved

Slurm launches tasks through the node's scheduler daemon, outside this client bubblewrap namespace. This can expose the normal host filesystem with the user's full filesystem permissions and consume project/allocation resources. **`--uenv-passthrough=ignore` discards the currently loaded uenv on the task; it is not itself a sandbox escape or a security boundary.** The outside execution comes from the Slurm launch. The task is not protected by the agent's bind restrictions.

Before `srun`, `sbatch`, or `salloc` for such work:

1. Describe the intended operation and why it cannot run here. State explicitly that it will execute outside the agent sandbox, with normal host filesystem access.
2. Determine the target cluster/node architecture, existing allocation if any, account, partition/QoS/reservation, nodes/tasks/CPUs/memory/time, exact shared working/build directories, output paths, and any required uenv image/view. Prefer the user's existing allocation; do not allocate another node automatically. `/dev/shm` and `/tmp` paths from this client are not shared source directories on another node.
3. Show the exact expanded command or complete job script. Use `--uenv-passthrough=ignore` when the task must not inherit this agent uenv; `srun` normally forwards it. If a different image is wanted, specify that image/view explicitly according to the live documentation instead of relying on inheritance. Confirm the target's Slurm/uenv plugin supports the flag; if not, hand off rather than trying to bypass it.
4. Review environment propagation. Do not export the whole agent environment blindly: sandbox `HOME`, XDG roots, `PATH`, library paths, runtime/tmp directories, private helper paths and credentials can be wrong or sensitive outside this namespace. Select an explicit environment allowlist and host-side paths, using the target Slurm's supported `--export` form. Do not place API keys in command lines, job scripts or logs. Never copy architecture-specific executable prefixes to a different architecture.
5. **Ask for explicit approval of the operation, outside-sandbox access, resource request, environment and exact paths. Wait.** No answer, a cancelled/unavailable question, or an error is not approval: finish with the plan and a plain-text question. A harmless read-only probe that launches a task still requires this approval.
6. After approval, launch only the reviewed command, observe its actual host/architecture/environment and output, and report job/step identity, status and artifacts. A changed destination, resource request, command or access scope requires renewed approval. Do not leave an unreported background job.

Review-only shape, not a command to execute with placeholders:

```text
srun <approved allocation/resource options> --uenv-passthrough=ignore \
  --chdir=<approved shared working directory> <approved export options> \
  /bin/bash --noprofile --norc -c '<approved command>'
```

For a long-lived agent/Paseo worker, use the native architecture's uenv and launcher in a tmux pane or an approved allocation. Start tmux before loading the uenv. Keep workers per-user and password-protected; connecting a UI does not authorize job submission or sharing a daemon with other users.
