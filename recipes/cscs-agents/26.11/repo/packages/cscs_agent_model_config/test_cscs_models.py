import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("cscs_models.py")
SPEC = importlib.util.spec_from_file_location("cscs_models_under_test", SCRIPT)
MODELS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODELS
SPEC.loader.exec_module(MODELS)


class ProductionConfigTest(unittest.TestCase):
    def run_generator(self, cli_args=(), environment=None, discovered=None):
        discovered = discovered if discovered is not None else [
            {"id": model_id} for model_id in MODELS.PUBLISHED_CONTEXT
        ]
        environment = environment if environment is not None else {
            MODELS.KEY_ENV: "test-secret-not-for-output"
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as output_dir:
            argv = [
                str(SCRIPT), "--out-dir", output_dir,
                "--no-docs", "--no-enrich", *cli_args,
            ]
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(MODELS, "SECRET_SH", Path("/nonexistent")),
                mock.patch.object(MODELS, "http_json", return_value=(200, {"data": discovered})) as request,
                mock.patch.object(MODELS, "http_get", side_effect=AssertionError("unexpected metadata GET")),
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                MODELS.main()
            request.assert_called_once_with(
                "https://api.inference.cscs.ch/v1/models",
                MODELS.auth(next(iter(environment.values()))),
            )
            files = {path.name: path.read_text() for path in Path(output_dir).iterdir()}
        for value in environment.values():
            self.assertNotIn(value, stdout.getvalue() + stderr.getvalue() + "".join(files.values()))
        return files, stdout.getvalue(), stderr.getvalue()

    def test_native_configs_use_production_and_documented_capabilities(self):
        files, _, _ = self.run_generator()
        opencode = json.loads(files[MODELS.OPENCODE_FILE])
        self.assertEqual(opencode["model"], "cscs/" + MODELS.DEFAULT_MODEL)
        self.assertEqual(opencode["agents"]["title"]["model"], opencode["model"])
        self.assertEqual(set(opencode["providers"]), {"cscs"})
        provider = opencode["providers"]["cscs"]
        self.assertEqual(provider["package"], "@opencode/ai/providers/anthropic")
        self.assertEqual(provider["settings"]["baseURL"], "https://api.inference.cscs.ch/v1")
        self.assertEqual(provider["settings"]["apiKey"], "{env:CSCS_INFERENCE_API_KEY}")
        self.assertEqual(len(provider["models"]), 7)
        for model_id, model in provider["models"].items():
            with self.subTest(model=model_id):
                self.assertNotIn("cost", model)
                self.assertTrue(model["capabilities"]["tools"])
                self.assertEqual(model["variants"], [])
                self.assertEqual(model["limit"]["context"], MODELS.PUBLISHED_CONTEXT[model_id])
                self.assertEqual(model["capabilities"]["input"],
                                 ["text"] if model_id == MODELS.SLOW_MODEL else ["text", "image"])
        self.assertIn("swiss-ai/Apertus-v1.5-8B", provider["models"])
        self.assertNotIn("swiss-ai/Apertus-v1.5-8B-thinking", provider["models"])

        omp = files[MODELS.OMP_FILE]
        self.assertNotIn("-thinking", omp)
        prime = json.loads(files[MODELS.PRIME_FILE])["providers"]
        self.assertEqual(set(prime), {"cscs"})
        self.assertEqual(prime["cscs"]["api"], "anthropic-messages")
        self.assertEqual(prime["cscs"]["apiKey"], "!printenv CSCS_INFERENCE_API_KEY")
        self.assertEqual(len(prime["cscs"]["models"]), 7)
        for model in prime["cscs"]["models"]:
            self.assertNotIn("cost", model)
            self.assertNotIn("audio", model["input"])
            self.assertNotIn("video", model["input"])

        claude = json.loads(files[MODELS.CLAUDE_SETTINGS_FILE])
        self.assertEqual(claude["env"]["ANTHROPIC_MODEL"], MODELS.DEFAULT_MODEL)
        self.assertEqual(claude["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], MODELS.SMOL_MODEL)
        self.assertEqual(claude["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"], MODELS.DEFAULT_MODEL)
        self.assertEqual(claude["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], MODELS.SLOW_MODEL)
        shell_aliases = {
            line.split("=", 1)[1].strip('"')
            for line in files[MODELS.CLAUDE_ENV_FILE].splitlines()
            if line.startswith("export ANTHROPIC_DEFAULT_")
        }
        self.assertEqual(shell_aliases, {MODELS.DEFAULT_MODEL, MODELS.SMOL_MODEL, MODELS.SLOW_MODEL})
        vscode = json.loads(files[MODELS.VSCODE_FILE])
        self.assertEqual(len(vscode), 1)
        self.assertEqual(vscode[0]["apiType"], "messages")
        self.assertTrue(all(model["url"] == "https://api.inference.cscs.ch/v1/messages"
                            for model in vscode[0]["models"]))

    def test_output_budget_ignores_discovery_limit_and_reserves_prompt_space(self):
        files, _, _ = self.run_generator(
            ["--format", "opencode", "--max-output", "200000"],
            discovered=[{"id": MODELS.SMOL_MODEL, "maxTokens": 262144}],
        )
        model = json.loads(files[MODELS.OPENCODE_FILE])["providers"]["cscs"]["models"][MODELS.SMOL_MODEL]
        self.assertEqual(model["limit"], {"context": 262144, "output": 131072})

    def test_automatic_selection_never_falls_back_to_apertus(self):
        for discovered, expected in (
            ([{"id": "swiss-ai/Apertus-v1.5-8B"}, {"id": MODELS.SLOW_MODEL}], MODELS.SLOW_MODEL),
            ([{"id": "swiss-ai/Apertus-v1.5-8B"}], None),
        ):
            with self.subTest(expected=expected):
                files, _, _ = self.run_generator(
                    ["--default-model", "swiss-ai/Apertus-v1.5-8B"], discovered=discovered,
                )
                opencode = json.loads(files[MODELS.OPENCODE_FILE])
                claude = json.loads(files[MODELS.CLAUDE_SETTINGS_FILE])
                if expected is None:
                    self.assertNotIn("model", opencode)
                    self.assertNotIn("agents", opencode)
                    self.assertEqual(claude["env"], {})
                    self.assertFalse(any(
                        line.startswith("export ANTHROPIC_DEFAULT_")
                        for line in files[MODELS.CLAUDE_ENV_FILE].splitlines()
                    ))
                else:
                    self.assertEqual(opencode["model"], "cscs/" + expected)
                    self.assertEqual(claude["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], expected)
                self.assertIn("swiss-ai/Apertus-v1.5-8B", opencode["providers"]["cscs"]["models"])

    def test_compatibility_credential_alias_uses_canonical_config_reference(self):
        files, _, _ = self.run_generator(
            ["--format", "opencode"], environment={"CSCS_API_KEY": "alias-test-secret"},
        )
        self.assertEqual(json.loads(files[MODELS.OPENCODE_FILE])["providers"]["cscs"]["settings"]["apiKey"],
                         "{env:CSCS_INFERENCE_API_KEY}")

    def test_missing_key_fails_without_contacting_service(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(MODELS, "SECRET_SH", Path("/nonexistent")),
            mock.patch.object(MODELS, "http_get") as request,
            mock.patch.object(sys, "argv", [str(SCRIPT), "--format", "table"]),
            self.assertRaisesRegex(SystemExit, "Set CSCS_INFERENCE_API_KEY"),
        ):
            MODELS.main()
        request.assert_not_called()

    def test_failed_catalogue_response_never_echoes_secret_body(self):
        gateway_args = type("GatewayArgs", (), {
            "provider_key": "cscs", "provider_name": "CSCS Inference", "label_suffix": " [CSCS]"
        })()
        for status, payload in ((401, "secret-response-body"), (200, "secret-response-body")):
            with self.subTest(status=status):
                with (
                    mock.patch.object(MODELS, "http_json", return_value=(status, payload)),
                    self.assertRaises(SystemExit) as error,
                ):
                    MODELS.list_models(MODELS.production_gateway(gateway_args), "test-secret")
                self.assertNotIn(payload, str(error.exception))

    def test_reports_keep_toolless_metadata_without_emitting_agent_models(self):
        _, stdout, _ = self.run_generator(["--format", "json"])
        rows = json.loads(stdout)
        self.assertEqual(len(rows), 9)
        thinking = next(row for row in rows if row["id"] == "swiss-ai/Apertus-v1.5-8B-thinking")
        self.assertFalse(thinking["tool_call"])
        self.assertTrue(thinking["vision"])

    def test_unknown_context_is_omitted_unless_explicitly_supplied(self):
        for extra_args, present in (([], False), (["--fallback-context", "8192"], True)):
            files, _, _ = self.run_generator(
                ["--format", "opencode", *extra_args], discovered=[{"id": "example/new-model"}],
            )
            providers = json.loads(files[MODELS.OPENCODE_FILE])["providers"]
            self.assertEqual("cscs" in providers, present)
            if present:
                self.assertEqual(providers["cscs"]["models"]["example/new-model"]["limit"],
                                 {"context": 8192, "output": 4096})

    def test_deployment_metadata_overrides_architectural_capabilities(self):
        args = type("Args", (), {"no_enrich": False, "refresh": False})()
        gateway_args = type("GatewayArgs", (), {
            "provider_key": "cscs", "provider_name": "CSCS Inference", "label_suffix": " [CSCS]"
        })()
        with mock.patch.object(MODELS, "hf_file", side_effect=[
            {"max_position_embeddings": 2097152, "vision_config": {}, "audio_config": {}}, {}
        ]):
            row = MODELS.describe(
                {"id": MODELS.SLOW_MODEL}, MODELS.PUBLISHED_CONTEXT,
                MODELS.PUBLISHED_INPUTS, MODELS.production_gateway(gateway_args), args,
            )
        self.assertEqual(row["context_window"], 1048576)
        self.assertFalse(row["vision"])
        self.assertFalse(row["audio"])

    def test_docs_cache_retains_snapshot_for_models_missing_from_documentation(self):
        html = (
            '<table><tr><th rowspan="2">Model</th><th rowspan="2">Maximum context length</th>'
            '<th colspan="4">Supported modalities</th></tr>'
            '<tr><th>Text</th><th>Image</th><th>Audio</th><th>Video</th></tr>'
            '<tr><td>example/new-model</td><td>65,536</td><td>✓</td><td>✓</td><td></td><td>✓</td></tr>'
            '</table>'
        )
        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                mock.patch.object(MODELS, "CACHE_DIR", Path(cache_dir)),
                mock.patch.object(MODELS, "http_get", return_value=(200, html)) as request,
            ):
                context, inputs, source = MODELS.fetch_published_metadata(MODELS.CSCS_DOCS, False)
                self.assertEqual(context[MODELS.DEFAULT_MODEL], 1048576)
                self.assertEqual(context["example/new-model"], 65536)
                self.assertEqual(inputs["example/new-model"], {"vision": True, "audio": False})
                cached_context, cached_inputs, cached_source = MODELS.fetch_published_metadata(MODELS.CSCS_DOCS, False)
                self.assertEqual(cached_context, context)
                self.assertEqual(cached_inputs, inputs)
                self.assertEqual(cached_source, source + " (cached)")
                request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
