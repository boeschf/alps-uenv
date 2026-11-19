#!/usr/bin/env python3
"""Generate production CSCS inference model configuration using metadata GETs.

The production /v1/models catalogue determines availability. The CSCS API docs
are authoritative for deployment context and supported input modalities; a
2026-11-17 snapshot supplies those values when documentation is unavailable.
Optional public Hugging Face metadata enriches architecture and sampling data.
No inference requests are sent and no API key is stored in generated files.

Outputs: OpenCode v2 opencode.json, Claude Code shell/settings blocks, VS Code
chatLanguageModels.json, Oh-My-Pi omp-models.yml and Prime models.json.
All clients use the production Anthropic Messages endpoint. Audio and video
are not advertised because this endpoint does not support them.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

CSCS_BASE = "https://api.inference.cscs.ch"
CSCS_DOCS = os.environ.get(
    "CSCS_INFERENCE_DOCS", "https://docs.cscs.ch/services/inference/api/"
)
HF_BASE = "https://huggingface.co"
SECRET_SH = Path.home() / ".local/share/secret-sh/secret.sh"
SECRET_NAME = "cscs-inference-api-key"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cscs-production-models"
TIMEOUT = 30

# Canonical credential names referenced by generated configs. Secret values are
# read only from the environment (or secret-sh for production) and never stored.
KEY_ENV = "CSCS_INFERENCE_API_KEY"
KEY_ALIASES = (KEY_ENV, "CSCS_API_KEY")


@dataclass(frozen=True)
class Gateway:
    provider_key: str
    provider_name: str
    base_url: str
    key_env: str
    label_suffix: str
    api: str
    opencode_package: str


def production_gateway(args) -> Gateway:
    return Gateway(
        provider_key=args.provider_key,
        provider_name=args.provider_name,
        base_url=f"{CSCS_BASE}/v1",
        key_env=KEY_ENV,
        label_suffix=args.label_suffix,
        api="anthropic-messages",
        opencode_package="@opencode/ai/providers/anthropic",
    )


# Published deployment metadata, snapshot 2026-11-17. Live documentation wins.
# https://docs.cscs.ch/services/inference/api/
PUBLISHED_CONTEXT = {
    "google/gemma-4-31B-it": 262144,
    "Qwen/Qwen3.8-27B-FP8": 262144,
    "Qwen/Qwen3.8-Flash-Next-FP8": 262144,
    "swiss-ai/Apertus-v1.5-70B": 262144,
    "swiss-ai/Apertus-v1.5-70B-thinking": 262144,
    "swiss-ai/Apertus-v1.5-8B": 262144,
    "swiss-ai/Apertus-v1.5-8B-thinking": 262144,
    "zai-org/GLM-5.3-Flash": 1048576,
    "zai-org/GLM-5.3": 1048576,
}
PUBLISHED_INPUTS = {
    model_id: {"vision": model_id != "zai-org/GLM-5.3",
               "audio": model_id.startswith("swiss-ai/Apertus-")}
    for model_id in PUBLISHED_CONTEXT
}
DEFAULT_MODEL = "zai-org/GLM-5.3-Flash"
SMOL_MODEL = "Qwen/Qwen3.8-27B-FP8"
SLOW_MODEL = "zai-org/GLM-5.3"

# "Note particularly that the -thinking variants of the Apertus models are
# served with tool use disabled." -- CSCS LLM Inference API docs. Agents that
# advertise tool calling against these get an "auto" tool choice requires
# --enable-auto-tool-choice error, so the configs must not claim tool support.
NO_TOOL_CALL = re.compile(r"apertus.*-thinking$", re.IGNORECASE)

# Tokenizers Oh-My-Pi ships, for model ids it cannot resolve on its own.
OMP_TOKENIZERS = (("glm-5", "glm5"), ("qwen3", "qwen3"))

# config.json keys that can hold the context window, in order of preference.
CONTEXT_KEYS = (
    "max_position_embeddings",
    "max_sequence_length",
    "max_seq_len",
    "n_positions",
    "seq_length",
)
# Nested sub-configs used by multimodal / composite models.
SUBCONFIGS = ("text_config", "llm_config", "language_config", "decoder")

# Sampling knobs vLLM picks up from generation_config.json (--generation-config
# auto is the default), i.e. the standard model defaults already in force.
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "repetition_penalty")


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
def get_api_key(
    aliases: tuple[str, ...], *, secret_name: str | None = None
) -> str | None:
    """Return a gateway key without logging or echoing it."""
    for name in aliases:
        key = os.environ.get(name)
        if key:
            return key.strip()
    if secret_name is None or not SECRET_SH.exists():
        return None
    # `secret` is a shell function, so it must be sourced before it can be called.
    proc = subprocess.run(
        ["bash", "-c", f'. "{SECRET_SH}" && secret dec {secret_name}'],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"error: could not decrypt '{secret_name}'")
    key = proc.stdout.strip()
    if not key:
        sys.exit(f"error: '{secret_name}' decrypted to an empty value")
    return key


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
def http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    """GET a URL as text. Returns (status, body|error-string)."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, str(exc)


def http_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, object]:
    """GET a URL and parse JSON. Returns (status, payload|error-string)."""
    status, body = http_get(url, headers)
    if status != 200:
        return status, body
    try:
        return status, json.loads(body)
    except json.JSONDecodeError as exc:
        return 0, str(exc)


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def list_models(gateway: Gateway, api_key: str) -> list[dict]:
    """Return the models reachable through one gateway."""
    url = f"{gateway.base_url}/models"
    status, payload = http_json(url, auth(api_key))
    if status != 200:
        sys.exit(f"error: GET {url} returned HTTP {status}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        sys.exit(f"error: unexpected {url} payload")
    return sorted(payload["data"], key=lambda m: m.get("id", ""))



# --------------------------------------------------------------------------- #
# published context lengths (CSCS docs)
# --------------------------------------------------------------------------- #
class ContextTableParser(HTMLParser):
    """Pull the `Model | Maximum context length` table out of the docs page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_context_table(html: str) -> dict[str, int]:
    """Model id -> maximum context length, from the docs' published table."""
    parser = ContextTableParser()
    parser.feed(html)

    header_seen = False
    published: dict[str, int] = {}
    for row in parser.rows:
        if len(row) < 2:
            continue
        first, second = row[0].lower(), row[1].lower()
        if "model" in first and "context" in second:
            header_seen = True
            continue
        if not header_seen:
            continue
        digits = re.sub(r"[^0-9]", "", row[1])
        if "/" in row[0] and digits:
            published[row[0].strip()] = int(digits)
        elif published:
            break  # table ended, don't wander into the next one
    return published


def parse_input_table(html: str) -> dict[str, dict[str, bool]]:
    """Read documented image/audio support, not architectural capabilities."""
    parser = ContextTableParser()
    parser.feed(html)
    columns = {}
    inputs = {}
    for row in parser.rows:
        lowered = [cell.lower() for cell in row]
        if "image" in lowered and "audio" in lowered:
            # The modality header follows the two rowspan model/context cells.
            columns = {name: lowered.index(name) + 2 for name in ("image", "audio")}
        elif columns and "/" in row[0] and len(row) > max(columns.values()):
            inputs[row[0].strip()] = {
                "vision": row[columns["image"]].strip() == "✓",
                "audio": row[columns["audio"]].strip() == "✓",
            }
    return inputs


def fetch_published_metadata(url: str, refresh: bool) -> tuple[dict, dict, str]:
    """Scrape deployment metadata, retaining snapshot entries missing in docs."""
    cache = CACHE_DIR / "docs" / (re.sub(r"\W+", "-", url).strip("-") + ".json")
    if cache.exists() and not refresh:
        try:
            cached = json.loads(cache.read_text())
            if cached.get("context"):
                return (
                    {**PUBLISHED_CONTEXT, **cached["context"]},
                    {**PUBLISHED_INPUTS, **cached.get("inputs", {})},
                    f"{url} (cached)",
                )
        except (json.JSONDecodeError, AttributeError):
            pass

    status, body = http_get(url, {"User-Agent": "cscs-models/2026.11"})
    if status != 200:
        return dict(PUBLISHED_CONTEXT), dict(PUBLISHED_INPUTS), f"built-in snapshot (docs HTTP {status})"
    published = parse_context_table(body)
    if not published:
        return dict(PUBLISHED_CONTEXT), dict(PUBLISHED_INPUTS), "built-in snapshot (no table on the docs page)"
    inputs = parse_input_table(body)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"context": published, "inputs": inputs}))
    return {**PUBLISHED_CONTEXT, **published}, {**PUBLISHED_INPUTS, **inputs}, url


# --------------------------------------------------------------------------- #
# hugging face enrichment
# --------------------------------------------------------------------------- #
def hf_file(repo: str, filename: str, refresh: bool) -> dict | None:
    """Fetch a JSON file from a HF repo, memoised on disk. None if unavailable."""
    cache = CACHE_DIR / repo.replace("/", "__") / filename
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text())
        except json.JSONDecodeError:
            pass  # corrupt cache entry, refetch

    headers = {"User-Agent": "cscs-models/2.0"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    status, payload = http_json(f"{HF_BASE}/{repo}/resolve/main/{filename}", headers)
    if status != 200 or not isinstance(payload, dict):
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload))
    return payload


def dig_context(cfg: dict) -> tuple[int | None, str | None]:
    """Find the context window in a config.json, returning (value, config-path)."""
    for key in CONTEXT_KEYS:
        val = cfg.get(key)
        if isinstance(val, int) and val > 0:
            return val, key
    for sub in SUBCONFIGS:
        nested = cfg.get(sub)
        if isinstance(nested, dict):
            val, path = dig_context(nested)
            if val is not None:
                return val, f"{sub}.{path}"
    return None, None


def dig(cfg: dict, key: str):
    """Look a key up at the top level, then in known sub-configs."""
    if key in cfg:
        return cfg[key]
    for sub in SUBCONFIGS:
        nested = cfg.get(sub)
        if isinstance(nested, dict) and key in nested:
            return nested[key]
    return None


def has_key(cfg: dict, *names: str) -> bool:
    """True if any of `names` appears at the top level or in a known sub-config."""
    for name in names:
        if cfg.get(name) is not None:
            return True
    for sub in SUBCONFIGS:
        nested = cfg.get(sub)
        if isinstance(nested, dict) and has_key(nested, *names):
            return True
    return False


def describe(
    model: dict,
    published: dict[str, int],
    published_inputs: dict[str, dict[str, bool]],
    gateway: Gateway,
    args,
) -> dict:
    """Build the settings record for one model id on one gateway."""
    repo = model.get("id", "")
    row: dict = {
        "provider_key": gateway.provider_key,
        "provider_name": gateway.provider_name,
        "base_url": gateway.base_url,
        "api_key_env": gateway.key_env,
        "api": gateway.api,
        "opencode_package": gateway.opencode_package,
        "label_suffix": gateway.label_suffix,
        "id": repo,
        "owned_by": model.get("owned_by"),
        "created": model.get("created"),
        "context_window": published.get(repo),
        "context_source": "cscs-docs" if repo in published else None,
        "architectural_context": None,
        "max_output_tokens": None,
        "max_output_source": None,
        "sampling_defaults": {},
        "sliding_window": None,
        "architecture": None,
        "model_type": None,
        "torch_dtype": None,
        "vocab_size": None,
        "rope_scaling_factor": None,
        "vision": published_inputs.get(repo, {}).get("vision", False),
        "audio": published_inputs.get(repo, {}).get("audio", False),
        "reasoning": "thinking" in repo.lower() or "-think" in repo.lower(),
        "tool_call": not NO_TOOL_CALL.search(repo),
        "gateway_source": model.get(
            "_metadata_source", f"{gateway.base_url}/models"
        ),
        "metadata_source": None,
        "notes": [],
    }

    if not row["tool_call"]:
        row["notes"].append(
            "served with tool use disabled; not usable in agent mode "
            "(CSCS docs, 'Apertus models in agents')"
        )

    if args.no_enrich:
        return row

    cfg = hf_file(repo, "config.json", args.refresh)
    if cfg is None:
        if row["context_window"] is None:
            row["notes"].append(
                "no published context length and no public config.json on Hugging "
                "Face (repo gated, private, or renamed); set HF_TOKEN if you have access"
            )
        return row

    row["metadata_source"] = f"huggingface.co/{repo}/config.json"
    arch_ctx, ctx_key = dig_context(cfg)
    row["architectural_context"] = arch_ctx
    if row["context_window"] is None and arch_ctx:
        # CSCS did not publish this one: fall back to the architectural maximum,
        # which is an upper bound on whatever the deployment actually serves.
        row["context_window"] = arch_ctx
        row["context_source"] = f"huggingface config.json:{ctx_key}"
        row["notes"].append(
            "not in the CSCS table; context is the model's architectural maximum, "
            "which the deployment may not serve in full"
        )
    elif arch_ctx and row["context_window"] and arch_ctx > row["context_window"]:
        row["notes"].append(
            f"CSCS serves {row['context_window']:,} of the model's "
            f"{arch_ctx:,} architectural window"
        )

    arch = cfg.get("architectures") or dig(cfg, "architectures")
    row["architecture"] = arch[0] if isinstance(arch, list) and arch else arch
    row["model_type"] = cfg.get("model_type")
    row["torch_dtype"] = cfg.get("torch_dtype") or cfg.get("dtype") or dig(cfg, "torch_dtype")
    row["vocab_size"] = dig(cfg, "vocab_size")

    if repo not in published_inputs:
        row["vision"] = has_key(cfg, "vision_config", "image_token_id", "image_token_index")
        row["audio"] = has_key(cfg, "audio_config", "audio_token_id", "audio_token_index")
    if row["audio"]:
        row["notes"].append(
            "accepts audio input, but generated agent configurations deliberately "
            "advertise only text and image inputs"
        )

    sw = dig(cfg, "sliding_window")
    if isinstance(sw, int) and sw > 0:
        row["sliding_window"] = sw
        row["notes"].append(f"interleaved sliding-window attention ({sw} tokens)")

    rope = dig(cfg, "rope_scaling")
    if isinstance(rope, dict):
        factor = rope.get("factor") or rope.get("rope_factor")
        row["rope_scaling_factor"] = factor
        if factor:
            row["notes"].append(f"context extended via RoPE scaling x{factor}")

    # Output cap and sampling defaults. vLLM loads generation_config.json by
    # default, so whatever is in there is what CSCS serves with. Only
    # `max_new_tokens` is a genuine cap on generated tokens; `max_length` bounds
    # prompt + output together, so it constrains the usable window instead.
    gen = hf_file(repo, "generation_config.json", args.refresh)
    if isinstance(gen, dict):
        new_tokens = gen.get("max_new_tokens")
        if isinstance(new_tokens, int) and new_tokens > 0:
            row["max_output_tokens"] = new_tokens
            row["max_output_source"] = "generation_config.json:max_new_tokens"
        total = gen.get("max_length")
        ctx = row["context_window"]
        if isinstance(total, int) and total > 0 and (ctx is None or total < ctx):
            row["notes"].append(
                f"generation_config.json caps prompt+output at {total} tokens"
            )
        row["sampling_defaults"] = {
            k: gen[k] for k in SAMPLING_KEYS if isinstance(gen.get(k), (int, float))
        }
    return row


# --------------------------------------------------------------------------- #
# derived limits
# --------------------------------------------------------------------------- #
def limits(row: dict, args) -> tuple[int | None, int | None]:
    """Resolve (context, output budget) for a client config, or (None, None).

    Context is what CSCS publishes. CSCS publishes no output cap and none of
    these models ships a hard one, so the output figure is the client-side
    budget from --max-output -- unless the model's own generation_config.json
    names a real `max_new_tokens`, which wins.

    Either way the budget is capped at half the context. Prompt and completion
    share one window here, and clients derive the input allowance from the
    difference: VS Code computes maxInputTokens as contextWindow -
    maxOutputTokens, so a budget equal to the context would leave no room for
    a prompt at all.
    """
    ctx = row["context_window"] or (args.fallback_context or None)
    if ctx is None:
        return None, None
    requested = row["max_output_tokens"] or args.max_output
    return ctx, max(1024, min(requested, ctx // 2))


def agent_models(rows: list[dict], args) -> list[dict]:
    """Rows an agent can actually drive: known limits, and tool calling."""
    return [
        row for row in rows
        if limits(row, args)[0] is not None and row["tool_call"]
    ]


def pick_default(rows: list[dict], preferred: str) -> dict | None:
    """Select automatic roles only from non-Apertus, tool-capable models."""
    automatic = [
        row for row in rows
        if row["tool_call"] and "apertus" not in row["id"].lower()
    ]
    for model_id in dict.fromkeys((preferred, DEFAULT_MODEL, SMOL_MODEL, SLOW_MODEL)):
        for row in automatic:
            if row["id"] == model_id:
                return row
    return automatic[0] if automatic else None




# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
def human(n) -> str:
    if not isinstance(n, int):
        return "-"
    if n >= 1048576 and n % 1048576 == 0:
        return f"{n // 1048576}M"
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}k"
    return str(n)


def short_name(model_id: str, suffix: str) -> str:
    """Append a display suffix to the model's basename."""
    return model_id.rsplit("/", 1)[-1] + suffix


def gateway_groups(rows: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Group rows by provider while preserving discovery order."""
    groups: dict[str, tuple[dict, list[dict]]] = {}
    for row in rows:
        key = row["provider_key"]
        if key not in groups:
            groups[key] = (row, [])
        groups[key][1].append(row)
    return list(groups.values())


def modalities(row: dict) -> list[str]:
    """Inputs the configs may advertise.

    Audio is deliberately left out even when public model metadata advertises
    it because client support differs across the generated API adapters.
    """
    return ["text"] + (["image"] if row["vision"] else [])


FIELDS = [
    "provider_key", "provider_name", "base_url", "api_key_env", "api",
    "id", "context_window", "context_source", "architectural_context",
    "max_output_tokens", "max_output_source", "sampling_defaults",
    "sliding_window", "architecture", "model_type", "torch_dtype", "vocab_size",
    "rope_scaling_factor", "vision", "audio", "reasoning", "tool_call",
    "gateway_source",
    "metadata_source", "owned_by", "created", "notes",
]

OPENCODE_FILE = "opencode.json"
CLAUDE_ENV_FILE = "claude-code.sh"
CLAUDE_SETTINGS_FILE = "claude-code.settings.json"
VSCODE_FILE = "chatLanguageModels.json"
OMP_FILE = "omp-models.yml"
PRIME_FILE = "prime-agent-models.json"


# --------------------------------------------------------------------------- #
# opencode
# --------------------------------------------------------------------------- #
def build_opencode(rows: list[dict], default: dict | None, args) -> dict:
    """Build native OpenCode v2 providers with explicit gateway compatibility."""
    providers: dict[str, dict] = {}
    for gateway, gateway_rows in gateway_groups(rows):
        models: dict[str, dict] = {}
        for row in gateway_rows:
            entry: dict = {"name": short_name(row["id"], row["label_suffix"])}
            ctx, out = limits(row, args)
            if ctx is not None:
                # OpenCode requires both fields when `limit` is present.
                entry["limit"] = {"context": ctx, "output": out}
            entry["capabilities"] = {
                "tools": bool(row["tool_call"] and args.tool_call),
                "input": modalities(row),
                "output": ["text"],
            }
            # The gateways apply their own thinking defaults. Inferring effort
            # variants from the adapter adds unsupported adaptive-thinking
            # parameters even to background title-generation requests.
            entry["variants"] = []
            models[row["id"]] = entry

        options = {
            "baseURL": gateway["base_url"],
            "apiKey": f"{{env:{gateway['api_key_env']}}}",
            "systemMessageMode": "system",
        }
        providers[gateway["provider_key"]] = {
            "package": gateway["opencode_package"],
            "name": gateway["provider_name"],
            "settings": options,
            "models": models,
        }

    config = {
        "$schema": "https://opencode.ai/v2/config.json",
        "permissions": [
            {"action": "external_directory", "resource": "*", "effect": "ask"},
        ],
    }
    if default is not None:
        config["model"] = f"{default['provider_key']}/{default['id']}"
        config["agents"] = {"title": {"model": config["model"]}}
    config["providers"] = providers
    return config


# --------------------------------------------------------------------------- #
# claude code
# --------------------------------------------------------------------------- #
def claude_env(row: dict, roles: dict, args, *, with_token: bool) -> dict[str, str]:
    """The env Claude Code needs to drive one CSCS model.

    `with_token` covers the shell snippet, where
    ANTHROPIC_AUTH_TOKEN=$CSCS_INFERENCE_API_KEY expands. A settings.json `env`
    block takes literal strings only -- no shell expansion -- so there the
    credential comes from `apiKeyHelper` instead.
    """
    ctx, out = limits(row, args)
    env = {}
    if with_token:
        env["ANTHROPIC_AUTH_TOKEN"] = f"${row['api_key_env']}"
    # ANTHROPIC_BASE_URL is the host: Claude Code appends /v1/messages itself.
    env["ANTHROPIC_BASE_URL"] = row["base_url"].removesuffix("/v1")
    env["ANTHROPIC_MODEL"] = row["id"]
    # Keep automatic aliases independent of a manually selected Apertus model.
    for alias, model in roles.items():
        if model is not None:
            env[f"ANTHROPIC_DEFAULT_{alias}_MODEL"] = model["id"]
    if ctx is not None:
        # The id is not a Claude one and carries no [1m] marker, so Claude Code
        # takes this value directly and compacts against it.
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(ctx)
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(out)
    if not row["reasoning"]:
        # On third-party providers 0 omits `thinking` rather than disabling it,
        # which is what a model without extended thinking needs.
        env["MAX_THINKING_TOKENS"] = "0"
    # CSCS serves /v1/models, so the picker can list the whole catalogue.
    env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    return env


def build_claude_env_script(rows, default, roles, args) -> str:
    lines = [
        "# Claude Code against the CSCS inference service.",
        "#",
        f"# Source the block for the model you want, then run `claude`. Set {KEY_ENV}",
        "# first -- it is the only secret involved, and it is never stored here.",
        "#",
        "# CLAUDE_CODE_MAX_CONTEXT_TOKENS carries the context length CSCS publishes.",
        "# Without it Claude Code guesses a window for an unrecognised model id and",
        "# compacts at the wrong point. CLAUDE_CODE_MAX_OUTPUT_TOKENS is a client-side",
        "# budget (CSCS publishes no output cap); raising it eats into the context",
        "# available before auto-compaction runs.",
        "#",
        "# Add CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 if you want no telemetry or",
        "# auto-updates -- it also switches off the gateway model discovery enabled",
        "# below, so the /model picker falls back to the pinned aliases.",
        "#",
        f"# {CLAUDE_SETTINGS_FILE} carries the same settings for the default model. It",
        f"# leaves ANTHROPIC_AUTH_TOKEN out on purpose: a settings.json env block stores",
        "# literal strings and does not expand shell variables, so the key comes from",
        "# apiKeyHelper there instead.",
        "",
    ]
    for row in rows:
        ctx, out = limits(row, args)
        marker = "  <- default" if default is not None and row["id"] == default["id"] else ""
        lines.append(
            f"# --- {row['id']} (context {ctx:,}, output budget {out:,}){marker}"
        )
        for note in row["notes"]:
            lines.append(f"#     note: {note}")
        for key, value in claude_env(row, roles, args, with_token=True).items():
            lines.append(f'export {key}="{value}"')
        lines.append("")

    if not rows:
        lines.append("# No model had a resolvable context length.")
        lines.append("")
    return "\n".join(lines)


def build_claude_settings(default, roles, args) -> dict:
    """The same configuration as a Claude Code settings.json block.

    Covers the default model only -- the context and output keys describe one
    active model, so a settings file pins one. apiKeyHelper supplies the
    credential: its output is sent as both X-Api-Key and Authorization: Bearer,
    and the CSCS gateway accepts either.
    """
    settings: dict = {"apiKeyHelper": args.api_key_helper}
    if default is None:
        settings["env"] = {}
        return settings
    settings["env"] = claude_env(default, roles, args, with_token=False)
    return settings


# --------------------------------------------------------------------------- #
# vs code
# --------------------------------------------------------------------------- #
def build_vscode(rows: list[dict], args) -> list[dict]:
    """Build one VS Code Custom Endpoint entry per gateway."""
    providers = []
    for gateway, gateway_rows in gateway_groups(rows):
        api_type = "messages"
        endpoint = "messages"
        models = []
        for row in gateway_rows:
            entry: dict = {
                "id": row["id"],
                "name": short_name(row["id"], row["label_suffix"]),
                "url": f"{row['base_url']}/{endpoint}",
                "toolCalling": bool(row["tool_call"] and args.tool_call),
                "vision": bool(row["vision"]),
            }
            ctx, out = limits(row, args)
            if ctx is not None:
                entry["contextWindow"] = ctx
                entry["maxOutputTokens"] = out
            if row["reasoning"]:
                entry["thinking"] = True
            models.append(entry)

        input_name = "cscsApiKey"
        providers.append({
            "name": gateway["provider_name"],
            "vendor": "customendpoint",
            "apiKey": f"${{input:{input_name}}}",
            "apiType": api_type,
            "models": models,
        })
    return providers


# --------------------------------------------------------------------------- #
# pi / oh-my-pi
# --------------------------------------------------------------------------- #
def omp_tokenizer(model_id: str) -> str | None:
    lowered = model_id.lower()
    for needle, tokenizer in OMP_TOKENIZERS:
        if needle in lowered:
            return tokenizer
    return None


def yaml_scalar(value) -> str:
    """Quote only what YAML would otherwise misread."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text or re.search(r"[:#{}\[\],&*?|<>=!%@`'\"]|^\s|\s$", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def build_omp(rows: list[dict], args) -> str:
    """Build Oh-My-Pi providers for ~/.omp/agent/models.yml."""
    out_lines = [
        "# CSCS production inference for Oh-My-Pi.",
        "# `apiKey` names an environment variable; no key is stored here.",
        "# Anthropic Messages supports text and image inputs.",
        "# contextWindow is deployment metadata where available; maxTokens is",
        "# a client-side output budget.",
        "providers:",
    ]
    for gateway, gateway_rows in gateway_groups(rows):
        out_lines.extend([
            f"  {gateway['provider_key']}:",
            f"    baseUrl: {gateway['base_url']}",
            f"    apiKey: {gateway['api_key_env']}",
            f"    api: {gateway['api']}",
            "    authHeader: true",
            "    disableStrictTools: true",
            "    models:",
        ])
        for row in gateway_rows:
            ctx, out = limits(row, args)
            fields = [
                ("id", row["id"]),
                ("name", short_name(row["id"], row["label_suffix"])),
                ("reasoning", bool(row["reasoning"])),
                ("contextWindow", ctx),
                ("maxTokens", out),
            ]
            tokenizer = omp_tokenizer(row["id"])
            if tokenizer:
                fields.insert(3, ("tokenizer", tokenizer))

            for note in row["notes"]:
                out_lines.append(f"      # {note}")
            head, *rest = fields
            out_lines.append(f"      - {head[0]}: {yaml_scalar(head[1])}")
            for key, value in rest:
                out_lines.append(f"        {key}: {yaml_scalar(value)}")
            out_lines.append(
                "        input: [" + ", ".join(modalities(row)) + "]"
            )
    out_lines.append("")
    return "\n".join(out_lines)


def build_prime_agent(rows: list[dict], args) -> dict:
    """Build Prime Agent's native custom-provider models.json."""
    providers = {}
    for gateway, gateway_rows in gateway_groups(rows):
        models = []
        for row in gateway_rows:
            ctx, out = limits(row, args)
            entry = {
                "id": row["id"],
                "name": short_name(row["id"], row["label_suffix"]),
                "reasoning": bool(row["reasoning"]),
                "input": modalities(row),
                "contextWindow": ctx,
                "maxTokens": out,
            }
            models.append(entry)

        providers[gateway["provider_key"]] = {
            "name": gateway["provider_name"],
            # Prime's Anthropic client appends /v1/messages, unlike OMP/OpenCode.
            "baseUrl": gateway["base_url"].removesuffix("/v1"),
            # Prime Agent treats an unknown bare name as a literal key. A command
            # returns no credential when the variable is unset.
            "apiKey": f"!printenv {gateway['api_key_env']}",
            "api": gateway["api"],
            "models": models,
        }
    return {"providers": providers}


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def print_table(rows: list[dict], context_source: str, args) -> None:
    cols = [
        ("PROVIDER", lambda r: r["provider_key"]),
        ("MODEL", lambda r: r["id"]),
        ("CONTEXT", lambda r: human(r["context_window"])),
        ("SOURCE", lambda r:
            "cscs" if r["context_source"] == "cscs-docs" else
            "hf" if r["context_source"] and r["context_source"].startswith("huggingface") else "-"),
        ("ARCH MAX", lambda r: human(r["architectural_context"])),
        ("OUT BUDGET", lambda r: human(limits(r, args)[1])),
        ("TOOLS", lambda r: "yes" if r["tool_call"] else "NO"),
    ]
    table = [[h for h, _ in cols]] + [[f(r) for _, f in cols] for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    for i, row in enumerate(table):
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))

    sources = ", ".join(
        f"{gateway['provider_key']}={len(gateway_rows)}"
        for gateway, gateway_rows in gateway_groups(rows)
    )
    print(f"\n{len(rows)} model(s): {sources}")
    print("CONTEXT source 'cscs': maximum context length published by CSCS at")
    print(f"  {context_source}")
    print(
        "  -- the window the production deployment serves. 'hf' is the public\n"
        "  architectural maximum and an upper bound on a deployment.\n"
        "  ARCH MAX is shown for comparison.\n"
        "OUT BUDGET is a client-side generation budget clamped to the context."
    )
    flagged = [r for r in rows if r["notes"]]
    if flagged:
        print("\nNotes:")
        for r in flagged:
            for note in r["notes"]:
                print(f"  {r['provider_key']}/{r['id']}: {note}")


def write_or_print(text: str, filename: str, args) -> None:
    if args.stdout:
        print(f"// ---- {filename} ----")
        print(text, end="" if text.endswith("\n") else "\n")
        return
    path = Path(args.out_dir).expanduser() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"wrote {path}", file=sys.stderr)


def write_json(payload, filename: str, args) -> None:
    write_or_print(json.dumps(payload, indent=2) + "\n", filename, args)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
FORMATS = ("configs", "opencode", "claude-code", "vscode", "pi", "table", "json", "csv")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--format", choices=FORMATS, default="configs",
        help="configs (default) writes every client config; opencode/claude-code/"
             "vscode/pi write one; table/json/csv report the raw model settings",
    )
    parser.add_argument(
        "--out-dir", default=".",
        help="directory to write the generated config files into (default: .)",
    )
    parser.add_argument(
        "--stdout", action="store_true",
        help="print the generated configs instead of writing files",
    )
    parser.add_argument(
        "--max-output", type=int, default=32768, metavar="N",
        help="client-side output-token budget, capped at half of each model's "
             "published context so a prompt still fits. CSCS publishes no output "
             "cap, so this is your choice, not the server's (default: 32768)",
    )
    parser.add_argument(
        "--fallback-context", type=int, default=0, metavar="N",
        help="context to assume for models CSCS does not publish and whose metadata "
             "could not be resolved; 0 (default) leaves them out of the configs "
             "rather than guessing",
    )
    parser.add_argument(
        "--default-model", default=DEFAULT_MODEL, metavar="ID",
        help=f"preferred non-Apertus automatic model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--api-key-helper", default=f"printenv {KEY_ENV}", metavar="CMD",
        help="command Claude Code's settings.json runs to obtain the key, since an "
             f"env block cannot expand ${KEY_ENV} itself "
             f"(default: printenv {KEY_ENV})",
    )
    parser.add_argument("--provider-key", default="cscs", help="provider key / group name (default: cscs)")
    parser.add_argument("--provider-name", default="CSCS Inference", help="provider display name")
    parser.add_argument("--label-suffix", default=" [CSCS]", help="suffix appended to each model label")
    parser.add_argument(
        "--no-tool-call", dest="tool_call", action="store_false",
        help="do not advertise tool calling at all (VS Code hides non-tool-calling "
             "models from agent mode)",
    )
    parser.add_argument(
        "--docs-url", default=CSCS_DOCS, metavar="URL",
        help=f"page carrying the published context-length table (default: {CSCS_DOCS})",
    )
    parser.add_argument(
        "--no-docs", action="store_true",
        help="skip the docs lookup and use the built-in snapshot of the published "
             "context lengths",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help=f"bypass the metadata cache in {CACHE_DIR}",
    )
    parser.add_argument(
        "--no-enrich", action="store_true",
        help="use only what CSCS itself publishes, with no Hugging Face lookups",
    )
    args = parser.parse_args()

    production_key = get_api_key(KEY_ALIASES, secret_name=SECRET_NAME)
    if production_key is None:
        sys.exit(f"error: no CSCS inference key is available. Set {KEY_ENV}=...")

    if args.no_docs:
        published, published_inputs, context_source = (
            dict(PUBLISHED_CONTEXT),
            dict(PUBLISHED_INPUTS),
            "built-in snapshot (--no-docs)",
        )
    else:
        published, published_inputs, context_source = fetch_published_metadata(
            args.docs_url, args.refresh
        )

    production = production_gateway(args)
    work = [
        (model, published, published_inputs, production)
        for model in list_models(production, production_key)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(
            pool.map(
                lambda item: describe(*item, args),
                work,
            )
        )

    if args.format == "json":
        json.dump(rows, sys.stdout, indent=2)
        print()
        return
    if args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                **row,
                "notes": "; ".join(row["notes"]),
                "sampling_defaults": json.dumps(row["sampling_defaults"]),
            })
        return
    if args.format == "table":
        print_table(rows, context_source, args)
        return

    usable = agent_models(rows, args)
    default = pick_default(usable, args.default_model)
    roles = {
        "SONNET": default,
        "HAIKU": pick_default(usable, SMOL_MODEL),
        "OPUS": pick_default(usable, SLOW_MODEL),
    }

    if args.format in ("configs", "opencode"):
        write_json(build_opencode(usable, default, args), OPENCODE_FILE, args)
    if args.format in ("configs", "claude-code"):
        write_or_print(
            build_claude_env_script(
                usable, default, roles, args
            ),
            CLAUDE_ENV_FILE,
            args,
        )
        write_json(
            build_claude_settings(default, roles, args),
            CLAUDE_SETTINGS_FILE,
            args,
        )
    if args.format in ("configs", "vscode"):
        write_json(build_vscode(usable, args), VSCODE_FILE, args)
    if args.format in ("configs", "pi"):
        write_or_print(build_omp(usable, args), OMP_FILE, args)
        write_json(build_prime_agent(usable, args), PRIME_FILE, args)

    print(f"\nproduction context lengths from: {context_source}", file=sys.stderr)
    if default is not None:
        print(
            f"default model:        {default['provider_key']}/{default['id']}",
            file=sys.stderr,
        )
    background = roles["HAIKU"]
    if background is not None and background is not default:
        print(
            f"background model:     {background['id']} (Claude Code haiku alias)",
            file=sys.stderr,
        )

    dropped = [
        f"{row['provider_key']}/{row['id']}"
        for row in rows
        if row not in usable
    ]
    if dropped:
        print(
            f"\nnote: {len(dropped)} model(s) left out of the configs:\n  "
            + "\n  ".join(dropped)
            + "\n  Either CSCS serves them with tool use disabled or no context "
              "length could be resolved (set HF_TOKEN for gated repos, or pass "
              "--fallback-context N to assume a window).",
            file=sys.stderr,
        )
    print(
        f"\nreminder: no generated file contains an API key. Export {KEY_ENV} for "
        "production. OpenCode, Oh-My-Pi, and Prime Agent read that variable; "
        "VS Code prompts for the production key. Merge each block into your "
        "real config -- nothing was written to a live config location.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
