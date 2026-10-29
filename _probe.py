"""Прямой тест нашего warm-движка: _call_summary_llm идёт по кешу.

Создаём движок, подставляем захваченный запрос (как его отдаёт хук
pre_api_request) и проверяем, что суммирование читает историю из кеша.
"""
import sys
import time

sys.path.insert(0, "/usr/local/lib/hermes-agent")

from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "muse-spark-1.3-contributor"
r = resolve_runtime_provider(requested="opencode-go", target_model=MODEL)
base = (r.get("base_url") or "").rstrip("/")
key = r.get("api_key") or ""
mode = r.get("api_mode") or "chat_completions"
print(f"model={MODEL} mode={mode} base={base}\n")

turns = []
for i in range(10):
    turns.append({"role": "user", "content":
                  f"Шаг {i}. Проверь конфиг nginx, найди причину 502. " + "Сервис падает. " * 10})
    turns.append({"role": "assistant", "content": f"Шаг {i}: поднял таймаут. " * 7})

# Захват в том виде, в каком его отдаёт pre_api_request
if mode == "codex_responses":
    captured = {"model": MODEL, "input": turns, "instructions": "You are Hermes Agent."}
elif mode == "anthropic_messages":
    captured = {"model": MODEL, "system": "You are Hermes Agent.", "messages": turns}
else:
    captured = {"model": MODEL,
                "messages": [{"role": "system", "content": "You are Hermes Agent."}] + turns}

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "warm_engine_test", "/home/cline-user/.hermes/plugins/idle-compact/warm_engine.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

Engine = mod.IdleWarmCompressor.build()
eng = Engine(model=MODEL, base_url=base, api_key=key, provider="opencode-go",
             api_mode=mode, config_context_length=1_000_000)

# 1) Прогрев: тот же префикс без инструкции (как реальный ход пользователя)
import types, importlib.util as _ilu  # noqa: E402
_pkg = types.ModuleType("idle_compact")
_pkg.__path__ = ["/home/cline-user/.hermes/plugins/idle-compact"]
sys.modules["idle_compact"] = _pkg
_s = _ilu.spec_from_file_location("idle_compact.warm_client",
                                  "/home/cline-user/.hermes/plugins/idle-compact/warm_client.py")
wc = _ilu.module_from_spec(_s)
sys.modules["idle_compact.warm_client"] = wc
_s.loader.exec_module(wc)
warm_client = wc

warm_client.request_summary(
    captured=captured, prompt="ответь одним словом: ок", api_mode=mode,
    base_url=base, api_key=key, max_tokens=2500)
print("прогрев отправлен")

# 2) Warm-суммирование через движок
eng.begin_warm(captured, {"base_url": base, "api_mode": mode, "api_key": key})
t0 = time.time()
text = eng._call_summary_llm("Сожми весь диалог выше в чекпоинт: Goal / Current state / Next step.", t0)
stats = eng.end_warm()
print(f"\nвремя: {time.time()-t0:.1f} c")
print(f"usage: cache_read={stats.get('cache_read')} свежий_вход={stats.get('fresh_input')} "
      f"output={stats.get('output_tokens')}")
print(f"текст ({len(text)} символов):\n{text[:300]}")
