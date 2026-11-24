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

import importlib.util
import os
import sys
import types
from pathlib import Path

# Тесты не должны уходить в перезапуск под другой интерпретатор, когда
# импорт ядра дёргает prepare_launch (relaunch sys.argv под tools/python
# плодит вложенные прогоны сьюты и ломает счёт). Только для тестов.
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _find_agent_home() -> Path | None:
    env = os.environ.get("HERMES_AGENT_HOME", "").strip()
    if env:
        return Path(env)
    candidates = []
    # HERMES_HOME: документированный кандидат из докстринга модуля. Без него
    # на нестандартном доме ядро не находится, и тесты падают, хотя оно есть.
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        expanded = Path(os.path.expandvars(os.path.expanduser(hermes_home)))
        candidates.append(expanded / "hermes-agent")
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


def _plugin_package(pkg_name: str) -> types.ModuleType:
    """Пакет с `__path__` плагина: без него относительный импорт не идёт.

    Существующий модуль в sys.modules не трогаем: тест мог собрать его сам из
    подделок (`wepkg_fake.policy`, `wepkg_fake.warm_client`), и затирать его
    значит подменить проверяемый путь настоящим клиентом.
    """
    pkg = sys.modules.get(pkg_name)
    if pkg is None:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(PLUGIN_DIR)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
    return pkg


def load_plugin_root(name: str = "ic_under_test") -> types.ModuleType:
    """Загрузить `__init__.py` плагина как пакет, а не как модуль без родителей.

    `spec_from_file_location` без `submodule_search_locations` отдаёт модуль без
    `__path__`, и первая же строка `from .model_ttl import ...` падает с
    «attempted relative import with no known parent package». Ради pytest это
    ошибка сбора всего файла, а не падение одного теста.
    """
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_plugin_module(filename: str, name: str) -> types.ModuleType:
    """Загрузить файл плагина внутри пакета, чтобы шли его относительные импорты.

    `savings.py` делает `from .model_ttl import ...`, `warm_engine.py` —
    `from . import policy, warm_client`. Точка в имени (`we.warm_engine`) задаёт
    родителя, иначе родитель — `<name>_pkg`. Сам модуль перезагружается на каждый
    вызов, родитель переиспользуется, чтобы подделки теста оставались в силе.
    """
    parent = name.rsplit(".", 1)[0] if "." in name else f"{name}_pkg"
    _plugin_package(parent)
    mod_name = name if "." in name else f"{parent}.{Path(filename).stem}"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod
