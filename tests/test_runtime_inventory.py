#!/usr/bin/env python3
"""Offline tests for runtime reproducibility inventory (issue #8).

Proves manifests parse, inventory coverage is real, secret/state paths
are excluded by policy, input hashes validate, the version capture is
secret-free, and release-CLI vs inventory responsibilities stay distinct.
No network, no production, no mutation.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "operations" / "runtime-inventory.md"
INPUTS = ROOT / "ops" / "runtime-inputs.json"
CAPTURE = ROOT / "scripts" / "capture-runtime-versions.py"

REQUIRED_COMPONENTS = [
    "Morning", "Radar", "Analytics", "Story", "Draft Factory",
    "renderer", "FEED", "CAROUSEL", "STORY",
    "final publication", "release CLI",
]

FORBIDDEN_PATH_FRAGMENTS = [
    "social/state/",
    "run-outcomes",
    "publish-callback-receipts",
    "deploy-state",
    "candidate-queue",
    ".env",
    "telegram-owner",
    "presign",
    "oauth",
    "session",
]

ALLOWED_INPUT_PREFIXES = (
    "workspace/social/tools/",
    "workspace/social/references/",
    "workspace/social/ops/prompts/",
    "tests/fixtures/",
    "requirements-runtime.txt",
    "requirements-dev.txt",
    "scripts/capture-runtime-versions.py",
    "scripts/validate-offline.sh",
    ".github/workflows/ci.yml",
)


class RuntimeInventoryTests(unittest.TestCase):
    def test_dependency_manifests_exist_and_parse(self):
        rt = (ROOT / "requirements-runtime.txt").read_text()
        pins = [ln.strip() for ln in rt.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        self.assertTrue(pins, "runtime manifest must declare at least one pin")
        for pin in pins:
            name, sep, version = pin.partition("==")
            self.assertTrue(sep, f"runtime pin must be exact (NAME==VERSION): {pin!r}")
            self.assertTrue(name and version)
        dev = (ROOT / "requirements-dev.txt").read_text()
        self.assertIn("stdlib", dev.lower())

    def _norm(self, text: str) -> str:
        return " ".join(text.split())

    def test_inventory_covers_required_components(self):
        text = self._norm(INVENTORY.read_text()).lower()
        for component in REQUIRED_COMPONENTS:
            self.assertIn(component.lower(), text)

    def test_inventory_status_vocabulary(self):
        text = INVENTORY.read_text()
        for status in ("PINNED", "CAPTURED_NOT_PINNED", "HOST_PROVIDED",
                       "EXTERNAL_CONTROLLED", "UNRESOLVED"):
            self.assertIn(status, text)
        self.assertIn("never PINNED", text)

    def test_no_secret_state_paths_included(self):
        blob = (INVENTORY.read_text() + INPUTS.read_text()).lower()
        for fragment in ("zernio_analytics_api_token=", "zernio_draft_api_token=",
                         "publish_api_token=", "bearer ", "telegram-owner-id"):
            self.assertNotIn(fragment, blob)
        manifest = json.loads(INPUTS.read_text())
        for rel in manifest["inputs"]:
            low = rel.lower()
            for fragment in FORBIDDEN_PATH_FRAGMENTS:
                self.assertNotIn(fragment, low, f"forbidden input: {rel}")
            self.assertTrue(
                rel == ALLOWED_INPUT_PREFIXES[-1] or rel == ALLOWED_INPUT_PREFIXES[-2]
                or any(rel.startswith(p) if p.endswith("/") else rel == p
                       for p in ALLOWED_INPUT_PREFIXES),
                f"input outside allowlist: {rel}")

    def test_input_hashes_validate(self):
        manifest = json.loads(INPUTS.read_text())
        self.assertEqual(manifest["schema"], "nullone.runtime-inputs/v1")
        self.assertTrue(manifest["inputs"])
        for rel, entry in manifest["inputs"].items():
            p = ROOT / rel
            self.assertTrue(p.is_file(), f"missing input: {rel}")
            h = hashlib.sha256()
            h.update(p.read_bytes())
            self.assertEqual(h.hexdigest(), entry["sha256"], f"hash drift: {rel}")

    def test_external_capture_secret_free(self):
        cp = subprocess.run([sys.executable, str(CAPTURE)],
                            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertEqual(data["schema"], "nullone.runtime-versions/v1")
        for key in ("python", "pillow", "node", "openclaw", "claude", "git"):
            self.assertIn(key, data)
        blob = json.dumps(data).lower()
        for needle in ("token", "bearer", "secret", "password", "session", "oauth"):
            self.assertNotIn(needle, blob)
        for value in data.values():
            if isinstance(value, str):
                self.assertLessEqual(len(value), 200)

    def test_cli_and_inventory_responsibilities_distinct(self):
        release_doc = self._norm(
            (ROOT / "docs" / "deployment" / "release-cli.md").read_text())
        self.assertIn("does NOT install", release_doc)
        inventory = INVENTORY.read_text()
        self.assertIn("does not install host packages", inventory)
        # Inventory must not re-specify CLI internals.
        for internal in ("MANAGED_TARGET", "managed_modes", "BOOTSTRAP_REQUIRED"):
            self.assertNotIn(internal, inventory)

    def test_ci_installs_pinned_requirements(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("-r requirements-runtime.txt", ci)
        self.assertIn("-r requirements-dev.txt", ci)
        self.assertIn("INSTALLED_PILLOW_PIN_VERIFIED", ci)
        # No bare unpinned Pillow install may remain.
        for line in ci.splitlines():
            stripped = line.strip()
            if "pip install" in stripped and "Pillow" in stripped:
                self.assertIn("-r requirements", stripped,
                              f"bare Pillow install: {stripped}")

    def test_installed_pillow_matches_manifest(self):
        import PIL

        pins = {}
        for line in (ROOT / "requirements-runtime.txt").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "==" in line:
                name, _, version = line.partition("==")
                pins[name.strip().lower()] = version.strip()
        self.assertIn("pillow", pins)
        self.assertEqual(PIL.__version__, pins["pillow"])

    def test_font_drift_identity_captured(self):
        import importlib.util
        from importlib.machinery import SourceFileLoader

        loader = SourceFileLoader(
            "capture_versions_mod", str(ROOT / "scripts" / "capture-runtime-versions.py"))
        spec = importlib.util.spec_from_loader("capture_versions_mod", loader)
        cap = importlib.util.module_from_spec(spec)
        loader.exec_module(cap)
        fonts = cap.capture()["fonts"]
        for name in ("regular", "bold"):
            entry = fonts[name]
            self.assertTrue(entry["path"].endswith(".ttf"))
            self.assertTrue(entry["exists"], f"required font missing: {name}")
            self.assertRegex(entry["sha256"] or "", r"^[0-9a-f]{64}$")
        # Missing font fails closed with a clear shape (no exception).
        missing = cap._font_identity("/nonexistent/Nope.ttf")
        self.assertFalse(missing["exists"])
        self.assertIsNone(missing["sha256"])

    def test_no_font_binaries_committed(self):
        binaries = [str(p) for p in ROOT.rglob("*")
                    if p.is_file() and p.suffix.lower() in
                    (".ttf", ".otf", ".woff", ".woff2")
                    and ".git/" not in str(p)]
        self.assertEqual(binaries, [])

    def test_local_validator_claims_truthful(self):
        script = (ROOT / "scripts" / "validate-offline.sh").read_text()
        self.assertIn("LOCAL_OFFLINE_VALIDATION", script)
        self.assertIn("does NOT", script)
        self.assertIn("isolated Python environment", script)
        self.assertIn("INSTALLED_PILLOW_PIN_VERIFIED", script)
        self.assertNotIn("docker", script.lower())
        self.assertNotIn("pip install", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
