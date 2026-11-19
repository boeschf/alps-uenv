---
name: refresh-cscs-models
description: "Refresh CSCS production inference model lists, context windows, and provider configuration for OpenCode, OMP, or Prime Agent when their configuration is stale or missing."
---

# Refresh CSCS production models

Use the packaged `cscs-agent-model-config` command. It queries production model metadata only; it does not query or emit prices. Do not add experimental endpoints, providers, credentials, or pricing options.

- Production authentication uses `CSCS_INFERENCE_API_KEY`; `CSCS_API_KEY` is a compatibility alias. Ask the user to export a key if neither is available. Never print a key, put it into a command line, or save its value in configuration. Generated files reference the environment variable, not its value.
- These commands show production metadata or write generated files to the selected output directory. They may update `$XDG_CACHE_HOME/cscs-production-models` (falling back to `$HOME/.cache/cscs-production-models`). Explain these writes and ask for permission first. Show the absolute output directory and substitute that approved directory for `/tmp/cscs-models` below.

```bash
# Human-readable production model table; no generated config files.
cscs-agent-model-config --format table

# OpenCode provider block: opencode.json.
cscs-agent-model-config --format opencode --out-dir /tmp/cscs-models

# OMP omp-models.yml and Prime Agent prime-agent-models.json provider files.
cscs-agent-model-config --format pi --out-dir /tmp/cscs-models
```

Inspect the generated files before proposing replacement of active configuration. Preserve unrelated personal providers, role selections, and user edits; preview the diff and ask before changing those files. Use the production defaults chosen by the launcher; do not assign Apertus to any role automatically. Non-thinking Apertus is manual opt-in only, and thinking variants without tools are not coding-agent candidates.

The `opencode-bwrap`, `omp-bwrap`, and `prime-agent-bwrap` launchers seed production defaults and opportunistically refresh only hash-managed, unchanged model files when production credentials are available. They do not overwrite files edited by the user. If a managed file is stale because it was edited, explain this and propose an explicitly approved merge rather than deleting the management marker or forcing a reset. This metadata refresh is independent of CSCS skill installation: skills never update over the network at launcher startup.
