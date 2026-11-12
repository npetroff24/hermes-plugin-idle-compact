"""Общий бутстрап тестов: пути к плагину и к исходникам ядра Hermes.

Тесты должны собираться на любой машине, а не только там, где ядро лежит
в /usr/local/lib/hermes-agent. Порядок поиска:
  1. переменная окружения HERMES_AGENT_HOME;
  2. <HERMES_HOME>/hermes-agent (Windows: %LOCALAPPDATA%\\hermes\\hermes-agent);
  3. ~/.hermes/hermes-agent;
  4. /usr/local/lib/hermes-agent (Linux-докер).
Нет ни одного — тесты, которым нужно ядро, честно падают с ImportError,
а не «проходят» на пустом месте.
"""

import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _find_agent_home() -> Path | None:
    env = os.environ.get("HERMES_AGENT_HOME", "").strip()
    if env:
        return Path(env)
    candidates = []
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        candidates.append(Path(local) / "hermes" / "hermes-agent")
    candidates.append(Path.home() / ".hermes" / "hermes-agent")
    candidates.append(Path("/usr/local/lib/hermes-agent"))
    for cand in candidates:
        if (cand / "agent").is_dir():
            return cand
    return None


AGENT_HOME = _find_agent_home()
if AGENT_HOME is not None and str(AGENT_HOME) not in sys.path:
    sys.path.insert(0, str(AGENT_HOME))
