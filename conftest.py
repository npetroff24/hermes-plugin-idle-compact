"""pytest: не дать pytest импортировать корень плагина как тест-модуль.

Репозиторий целиком — это плагин, `tests/` лежит внутри него. pytest 9 заводит
узел `Package` для каталога с `__init__.py` и в setup импортирует этот файл по
пути. Имя каталога (`hermes-plugin-idle-compact`) не является идентификатором
Python, поэтому разрешить пакет по пути нельзя: pytest откатывается на имя
`__init__` и грузит плагин как модуль без родителей. Первая же строка
`from .model_ttl import ...` падает с «attempted relative import with no known
parent package», и каждый файл тестов получает ошибку сбора вместо прогона.

Имя занято заранее пустым пакетом: importlib берёт уже импортированный модуль из
sys.modules и второй раз не грузит. Пустым, а не настоящим, чтобы конфиг и ядро
не читались на этапе сбора: тестам, которым нужно ядро, нужен их собственный
честный ImportError, а не падение всей сьюты в conftest.
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if "__init__" not in sys.modules:
    _root_pkg = types.ModuleType("__init__")
    _root_pkg.__path__ = [str(ROOT)]
    _root_pkg.__package__ = ""
    sys.modules["__init__"] = _root_pkg
