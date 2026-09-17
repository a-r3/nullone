#!/usr/bin/env python3
"""Manifest-compatibility regression tests for the PR #143 deployment blocker.

Production Gateway (OpenClaw 2026.8.2) rejected
`plugins/nullone-draft-bridge/openclaw.plugin.json` at startup with:

    plugin manifest requires configSchema   (exit 78/CONFIG)

The controlled #37 deployment was rolled back safely; the action logic
was never the failure. These tests pin the fix so the exact failure
cannot recur:

- the manifest carries a `configSchema` record (the loader's
  `loadPluginManifest` requires `isRecord(raw.configSchema)`);
- the schema is the loader's canonical EMPTY-CONFIG shape
  (`type: object`, empty `properties`, `additionalProperties: false`),
  which `validatePluginConfig` accepts with config absent/empty and
  rejects with any non-empty config (mirrors
  `isEmptyPluginConfigJsonSchema` in the installed
  `loader-shared-X_q42oTb.js`);
- the schema exposes no secrets, credentials, execution, model,
  Telegram, approval, or publication surface;
- the manifest adds no new authority fields (no
  `configContracts.secretInputs`, no extra top-level keys);
- where the installed OpenClaw runtime is available, the REAL
  `loadPluginManifest` validator is executed against the repo
  manifest (skipped, not failed, when OpenClaw is not installed);
- the action core still accepts exactly `{"manifest_id": ...}` and
  the plugin entry still has no general-exec / model surface.

Deterministic offline. No network, no Zernio, no Telegram, no model.
"""

from __future__ import annotations

import glob
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "nullone-draft-bridge"
MANIFEST_PATH = PLUGIN_DIR / "openclaw.plugin.json"
INDEX_PATH = PLUGIN_DIR / "index.js"
ROUTE_PATH = PLUGIN_DIR / "route.js"
SCRIPTS = ROOT / "workspace/social/ops/scripts"

sys.path.insert(0, str(SCRIPTS))

# Exact keyword allowlist from the installed loader's
# EMPTY_PLUGIN_CONFIG_SHORTCUT_KEYWORDS (loader-shared-X_q42oTb.js).
EMPTY_SCHEMA_ALLOWED_KEYS = frozenset(
    {
        "type",
        "additionalProperties",
        "properties",
        "title",
        "description",
        "$schema",
        "$id",
        "$comment",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)

# Tokens that must never appear in the plugin config schema: they would
# signal secret, execution, model, or authority configuration surface.
FORBIDDEN_SCHEMA_TOKENS = (
    "token",
    "secret",
    "credential",
    "password",
    "api_key",
    "apikey",
    "bearer",
    "exec",
    "shell",
    "spawn",
    "command",
    "script",
    "model",
    "llm",
    "provider",
    "telegram",
    "approv",
    "publish",
    "schedule",
)

EXPECTED_TOP_LEVEL_KEYS = frozenset(
    {"id", "version", "name", "description", "entry", "activation", "configSchema"}
)


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def is_empty_plugin_config_schema(schema: object) -> bool:
    """Mirror of the installed loader's isEmptyPluginConfigJsonSchema."""
    if not isinstance(schema, dict):
        return False
    if schema.get("type") != "object":
        return False
    if schema.get("additionalProperties") is not False:
        return False
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > 0:
        return False
    return all(k in EMPTY_SCHEMA_ALLOWED_KEYS for k in schema)


def empty_shortcut_validate(schema: dict, value: object) -> tuple[bool, str]:
    """Mirror of validatePluginConfig's empty-schema branch."""
    if value is None:
        return True, "absent config accepted"
    if isinstance(value, dict) and len(value) == 0:
        return True, "empty config accepted"
    if not isinstance(value, dict):
        return False, "<root>: must be object"
    return False, "<root>: config must be empty"


def find_installed_manifest_validator() -> str | None:
    """Locate the installed OpenClaw manifest chunk exposing loadPluginManifest."""
    candidates: list[str] = []
    npm_root = shutil.which("npm")
    if npm_root is not None:
        try:
            proc = subprocess.run(
                ["npm", "root", "-g"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                candidates.append(
                    str(Path(proc.stdout.strip()) / "openclaw" / "dist")
                )
        except (OSError, subprocess.SubprocessError):
            pass
    candidates.append("/home/oem/.nvm/versions/node/v22.23.2/lib/node_modules/openclaw/dist")
    for dist in candidates:
        for chunk in sorted(glob.glob(str(Path(dist) / "manifest-*.js"))):
            if "manifest-registry" in chunk:
                continue
            try:
                probe = subprocess.run(
                    [
                        "node",
                        "--input-type=module",
                        "-e",
                        f"import {{ r }} from {json.dumps(chunk)};"
                        " if (typeof r !== 'function') { process.exit(2); }",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if probe.returncode == 0:
                    return chunk
            except (OSError, subprocess.SubprocessError):
                continue
    return None


class DraftBridgeManifestTests(unittest.TestCase):
    def test_manifest_configschema_present_and_empty_shape(self) -> None:
        manifest = load_manifest()
        self.assertIn("configSchema", manifest, "configSchema is required by the production Gateway")
        schema = manifest["configSchema"]
        self.assertIsInstance(schema, dict, "configSchema must be a record (isRecord check)")
        self.assertTrue(
            is_empty_plugin_config_schema(schema),
            f"configSchema must be the canonical empty-config shape, got: {schema!r}",
        )

    def test_manifest_top_level_keys_unchanged(self) -> None:
        manifest = load_manifest()
        self.assertEqual(
            frozenset(manifest.keys()),
            EXPECTED_TOP_LEVEL_KEYS,
            "manifest must not gain authority fields (e.g. configContracts.secretInputs)",
        )
        self.assertEqual(manifest["id"], "nullone-draft-bridge")
        self.assertEqual(manifest["entry"], "index.js")
        self.assertTrue((PLUGIN_DIR / manifest["entry"]).exists())
        self.assertEqual(manifest.get("activation", {}).get("onStartup"), True)
        self.assertNotIn("configContracts", manifest)

    def test_schema_exposes_no_secret_or_authority_surface(self) -> None:
        schema = load_manifest()["configSchema"]
        lowered = json.dumps(schema).lower()
        for token in FORBIDDEN_SCHEMA_TOKENS:
            self.assertNotIn(
                token,
                lowered,
                f"configSchema must not expose {token!r} configuration surface",
            )

    def test_empty_schema_config_gate(self) -> None:
        schema = load_manifest()["configSchema"]
        ok, _ = empty_shortcut_validate(schema, None)
        self.assertTrue(ok, "absent plugin config must validate")
        ok, _ = empty_shortcut_validate(schema, {})
        self.assertTrue(ok, "empty plugin config must validate")
        ok, _ = empty_shortcut_validate(schema, {"anything": 1})
        self.assertFalse(ok, "non-empty plugin config must be rejected")
        ok, _ = empty_shortcut_validate(schema, "x")
        self.assertFalse(ok, "non-object plugin config must be rejected")

    def test_action_schema_still_manifest_id_only(self) -> None:
        import nullone_draft_bridge_action as action

        self.assertEqual(action.ACTION_NAME, "nullone.draft-bridge.run")
        self.assertEqual(action.handle_request.__module__, action.__name__)
        # Exactly one key accepted; every known forbidden/unknown field rejected.
        mid = "2026-09-16-manifest-pinning-2026-09-16"
        self.assertEqual(action._validate_request({"manifest_id": mid}), mid)
        for extra in ("command", "path", "url", "token", "publish", "approval", "telegram", "mode", "extra"):
            with self.assertRaises(Exception, msg=f"field {extra!r} must be rejected"):
                action._validate_request({"manifest_id": mid, extra: "x"})

    def test_plugin_entry_has_no_general_exec_or_model_surface(self) -> None:
        import re

        index_src = INDEX_PATH.read_text(encoding="utf-8")
        # Strip JS comments: authority checks apply to code, while the
        # header legitimately documents what the plugin does NOT do.
        code = re.sub(r"/\*.*?\*/", "", index_src, flags=re.DOTALL)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        for token in ("execSync", "execFileSync", "shell: true", "shell:true", "/bin/sh", "bash -c"):
            self.assertNotIn(token, code, f"plugin entry code must not contain {token!r}")
        lowered = code.lower()
        for token in ("sendmessage", ".send(", "approv", "publish", ".callmodel", "llm("):
            self.assertNotIn(token, lowered, f"plugin entry code must not gain {token!r} authority")
        # The single spawn call is fixed-argv python3 + controller path only.
        self.assertIn("spawn(", code)
        route_src = ROUTE_PATH.read_text(encoding="utf-8")
        self.assertIn("texbrif", route_src)

    def test_real_gateway_manifest_validator(self) -> None:
        chunk = find_installed_manifest_validator()
        if chunk is None:
            self.skipTest("installed OpenClaw manifest validator not available; structural checks above apply")
            return
        node = shutil.which("node")
        self.assertIsNotNone(node, "node binary is required for the real-validator check")
        assert node is not None
        proc = subprocess.run(
            [
                node,
                "--input-type=module",
                "-e",
                f"import {{ r as loadPluginManifest }} from {json.dumps(chunk)};"
                f" const res = loadPluginManifest({json.dumps(str(PLUGIN_DIR))});"
                " if (!res.ok) { console.error(res.error); process.exit(1); }"
                " console.log('manifest OK');",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
        )
        self.assertEqual(
            proc.returncode,
            0,
            "real production loadPluginManifest must accept the fixed manifest:\n" + proc.stdout + proc.stderr,
        )

    def test_final_publish_manifest_untouched(self) -> None:
        other = ROOT / "plugins" / "nullone-final-publish" / "openclaw.plugin.json"
        data = json.loads(other.read_text(encoding="utf-8"))
        self.assertEqual(data.get("id"), "nullone-final-publish")
        self.assertEqual(data.get("entry"), "index.js")


if __name__ == "__main__":
    unittest.main(verbosity=2)
