# idle-compact

Фоновое сжатие простаивающих чатов Hermes-агента — до истечения prompt-cache.

*English: background compaction for idle Hermes chats before prompt-cache
expiry — zero waiting, cache-warm summaries (96–98% prefix hits vs ~0% for
stock aux calls), per-model TTLs (DeepSeek ~85 min, Spark/GPT/Gemini ~10 min,
others ~5 min), live gate from the core compressor, restart-proof timers,
Windows + Linux, and a SAVINGS ledger for every dollar.*

## Проблема

Длинные чаты упираются в лимит контекста, а штатное сжатие бьёт трижды:
срабатывает в момент вашего запроса — вы ждёте десятки секунд; считает
**на холодную** — aux-вызов идёт мимо prompt-cache (~0% попаданий по нашим
замерам), каждое сжатие оплачивается по полному тарифу; а долгий простой
протухает кеш окончательно — следующий ход перечитывает весь контекст
по полной цене.

## Преимущества

1. **Ноль ожидания.** Сжимает заранее, в простое — возвращаетесь, чат уже
   ужатый. Ждать сжатия не приходится вообще.
2. **Сжатие по кешу, а не мимо.** Запрос суммирования повторяет последний ход
   и попадает в prompt-cache на 96–98% вместо 0% — само сжатие стоит копейки.
3. **Момент подбирает сам.** У каждой модели свой TTL кеша: DeepSeek ~85 мин,
   Spark/GPT/Gemini ~10 мин, Claude/Kimi/Qwen/MiniMax и остальные ~5 мин.
   Старт за 10% до протухания (TTL × 0.9).
4. **Мелочь не жмёт.** Порог берёт у живого компрессора ядра (цель × 2) —
   короткие чаты пропускает, зря не тратит.
5. **Деньги видны.** После каждого сжатия пишет SAVINGS: было → стало, цена;
   на вашем возврате — тёплым или холодным был вход и сколько реально
   сэкономлено.
6. **Переживает рестарт.** Таймеры сохраняются на диск — рестарт шлюза сжатие
   не срывает.
7. **Windows и Linux.** Один код: лок через `msvcrt`/`fcntl`, пути через
   `%LOCALAPPDATA%`/`~/.hermes`.

Итого: меньше контекст — дешевле каждый ответ, ноль ожидания сжатия, каждая
цифра в логе.

## Замеры warm-пути (29.10.2026, opencode-go, живые запросы)

| модель | транспорт | cache_read | свежий вход | кеш |
|---|---|---|---|---|
| muse-spark-1.3-contributor | codex_responses | 1393 | 83 | 94% |
| deepseek-v4.1-flash | chat_completions | 1792 | 46 | 97.5% |
| minimax-m3.1-flash | anthropic_messages | 1920 | 153 | 92.6% |

Саммари при этом остаётся структурным (Goal / Current state / Next step),
tools и reasoning не ломаются.

## Логика

1. После каждого хода — таймер TTL × 0.9 (боевые TTL по таблице
   `TTL_BY_MODEL_SUBSTR`: deepseek 85 мин → 4590 сек, spark/gpt/gemini
   10 мин → 540 сек, остальные 5 мин → 270 сек).
2. Сработал: сессия тиха, источник human, размер > гейта
   (гейт = цель × 2, цель = живой `threshold_tokens × target_ratio`).
3. Плагин захватывает последний реальный запрос (хук `pre_api_request`) и
   включает warm-путь на своём контекст-движке.
4. Сжатие делает ядро (`resident._compress_context`): локи, cooldown,
   anti-thrash, watermark против гонок. Плагин подменяет только LLM-вызов
   суммирования — вся логика замены середины остаётся ядровой.
5. Warm недоступен (нет захвата, сменился маршрут, ошибка) — автоматический
   откат на штатный aux-путь.
6. Маленькие/молодые/cron-сессии не трогаются.

## Свой контекст-движок

Плагин регистрирует `idle_warm` — наследник штатного `ContextCompressor`,
который переопределяет единственный метод `_call_summary_llm`. Ничего в ядре
не патчится: обычное наследование публичного класса.

Если ядро сменит сигнатуру этого метода, плагин отключит warm и продолжит
работать через aux (проверка `inspect.signature` на старте).

Нужен `context.engine: idle_warm` в конфиге:

```bash
hermes config set context.engine idle_warm
```

## Установка

```bash
hermes plugins install <git-url> --enable
hermes config set context.engine idle_warm
```

## Откат

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
```

Сжатие архивирует строки (`active=0`), не удаляет.
