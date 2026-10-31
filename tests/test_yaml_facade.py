"""YAML facade tests: hermes_yaml instead of PyYAML on Python 3.14.

PyYAML ships no CPython 3.14 wheel and Hermes core installs ruamel under the
`hermes_yaml` name. The facade must prefer it, and fall back to PyYAML on a
standalone install. The plugin module is loaded as a package because it uses
relative imports.
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "icpkg_facade_under_test",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _real_yaml():
    """A YAML parser that actually exists on this machine, or None."""
    for name in ("hermes_yaml", "yaml"):
        try:
            __import__(name)
        except ImportError:
            continue
        return sys.modules[name]
    return None


class TestYamlFacade(unittest.TestCase):
    def setUp(self):
        self.mod = _load_plugin()
        self._saved = {name: sys.modules.pop(name, None) for name in ("hermes_yaml", "yaml")}

    def tearDown(self):
        for name, mod in self._saved.items():
            sys.modules.pop(name, None)
            if mod is not None:
                sys.modules[name] = mod

    def test_prefers_hermes_yaml(self):
        """The core module wins over PyYAML: 3.14 has no PyYAML wheel."""
        sys.modules["hermes_yaml"] = types.ModuleType("hermes_yaml")
        sys.modules["yaml"] = types.ModuleType("yaml")
        self.assertIs(self.mod._yaml_module(), sys.modules["hermes_yaml"])

    def test_falls_back_to_pyyaml(self):
        """A standalone install without the core module still reads config."""
        sys.modules["yaml"] = types.ModuleType("yaml")
        real_import = builtins.__import__

        def no_hermes_yaml(name, *args, **kwargs):
            if name == "hermes_yaml":
                raise ImportError("no hermes_yaml on this install")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", no_hermes_yaml):
            self.assertIs(self.mod._yaml_module(), sys.modules["yaml"])

    def test_reads_plugin_cfg(self):
        """_plugin_cfg reads config.yaml through the facade, not an import error."""
        if _real_yaml() is None:
            self.skipTest("neither hermes_yaml nor PyYAML is installed")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text("plugins:\n  idle_compact:\n    gate_fraction: 0.15\n", encoding="utf-8")
            self.mod._home = lambda: Path(tmp)
            self.assertEqual(self.mod._plugin_cfg(), {"gate_fraction": 0.15})
            self.assertEqual(self.mod._gate_fraction(), 0.15)


class TestNoBareYamlImport(unittest.TestCase):
    def test_only_facade_imports_yaml(self):
        """A bare `import yaml` is only allowed inside the facade itself."""
        src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8").splitlines()
        bare = [
            f"{i}: {line.strip()}"
            for i, line in enumerate(src, 1)
            if line.strip() == "import yaml"
        ]
        self.assertEqual(len(bare), 1, f"bare import yaml outside the facade: {bare}")
