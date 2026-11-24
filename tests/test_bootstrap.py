"""Поиск исходников ядра: порядок кандидатов совпадает с докстрингом модуля."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402


class TestAgentHomeSearch(unittest.TestCase):
    def _with_env(self, **env):
        """Окружение как в докстринге: override → HERMES_HOME → платформа."""
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("HERMES_AGENT_HOME", "HERMES_HOME", "LOCALAPPDATA"):
            if key not in env:
                self.addCleanup(os.environ.pop, key, None)
                os.environ.pop(key, None)

    def _agent_tree(self, root: Path) -> Path:
        (root / "agent").mkdir(parents=True)
        return root

    def test_hermes_home_candidate_is_used(self):
        """HERMES_HOME/hermes-agent — второй кандидат докстринга."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "custom-home"
            tree = self._agent_tree(home / "hermes-agent")
            self._with_env(HERMES_HOME=str(home))
            self.assertEqual(_bootstrap._find_agent_home(), tree)

    def test_explicit_agent_home_still_wins(self):
        """HERMES_AGENT_HOME остаётся первым и не требует проверки каталога."""
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit-agent"
            other_home = Path(tmp) / "other-home"
            self._agent_tree(other_home / "hermes-agent")
            self._with_env(HERMES_AGENT_HOME=str(explicit),
                           HERMES_HOME=str(other_home))
            self.assertEqual(_bootstrap._find_agent_home(), explicit)

    def test_platform_default_used_when_hermes_home_pointless(self):
        """HERMES_HOME без hermes-agent не заслоняет платформенный дефолт."""
        with tempfile.TemporaryDirectory() as tmp:
            empty_home = Path(tmp) / "empty-home"
            empty_home.mkdir()
            local = Path(tmp) / "localappdata"
            platform_tree = self._agent_tree(local / "hermes" / "hermes-agent")
            self._with_env(HERMES_HOME=str(empty_home), LOCALAPPDATA=str(local))
            with mock.patch.object(Path, "home", lambda: Path(tmp) / "nohome"):
                self.assertEqual(_bootstrap._find_agent_home(), platform_tree)

    def test_expands_vars_in_hermes_home(self):
        """Значение HERMES_HOME раскрывается как путь, а не берётся сырым."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = self._agent_tree(root / "hermes" / "hermes-agent")
            self._with_env(HERMES_HOME=str(root / "hermes"))
            with mock.patch.dict(os.environ, {"HOME": str(root)}):
                self.assertEqual(_bootstrap._find_agent_home(), tree)


if __name__ == "__main__":
    unittest.main(verbosity=2)
