"""Human-source tests: a Discord session must arm a timer.

Hermes stores a gateway turn with `source = session_source_for(platform)`, which
returns the platform name, so a Discord turn lands in `sessions.source` as
'discord'. The arming path compares that column against HUMAN_SOURCES and the
dry-run helper repeats the same allowlist in SQL. Both must cover the Discord
transport, or the plugin is enabled, hooked and inert on a Discord host.

The plugin module is loaded as a package because it uses relative imports.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "icpkg_human_sources_under_test",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_state_db(path: Path, sessions: list[tuple[str, str]], chars: int) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, "
            "message_count INTEGER, last_activity_at REAL, archived INTEGER, ended_at REAL)"
        )
        con.execute(
            "CREATE TABLE messages (session_id TEXT, content TEXT, active INTEGER)"
        )
        now = time.time()
        for session_id, source in sessions:
            con.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, 0, NULL)",
                (session_id, source, "deepseek-v4.1-flash", 2, now),
            )
            con.execute(
                "INSERT INTO messages VALUES (?, ?, 1)", (session_id, "x" * chars)
            )
        con.commit()
    finally:
        con.close()


class TestHumanSources(unittest.TestCase):
    def setUp(self):
        self.mod = _load_plugin()

    def test_discord_is_a_human_source(self):
        """The constant every arming path checks must include the Discord transport."""
        self.assertIn("discord", self.mod.HUMAN_SOURCES)

    def test_scan_candidates_lists_discord_session(self):
        """The dry-run helper's SQL must not hide Discord rows the constant accepts."""
        with tempfile.TemporaryDirectory() as tmp:
            _build_state_db(
                Path(tmp) / "state.db",
                [("discord-session", "discord"), ("cron-session", "cron")],
                chars=1_000_000,
            )
            self.mod._home = lambda: Path(tmp)
            rows = self.mod.scan_candidates()
        sources = {r["source"] for r in rows}
        self.assertIn("discord", sources)
        self.assertNotIn("cron", sources, "a cron session is not a human conversation")


if __name__ == "__main__":
    unittest.main()
