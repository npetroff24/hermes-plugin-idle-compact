# idle-compact

Background compaction for idle Hermes-agent chats — before prompt-cache expiry.

## The problem

Long chats hit the context limit, and stock compression hurts three times:
it fires mid-request — you wait tens of seconds; it computes **cold** — the
aux call bypasses prompt-cache (~0% hits by our measurements), so every
compaction is billed at full rate; and a long idle lets the cache expire
completely — your next turn re-reads the whole context at full price.

## Advantages

1. **No waiting when it finishes idle.** Compacts ahead of time, while idle —
   if compaction completes before you return, the next message never waits for it.
2. **Cache-warm, not cache-blind.** The summary request repeats your last turn
   and hits prompt-cache at 92.6–97.5% in three measurements instead of ~0% —
   the compaction itself costs pennies.
3. **Timing per model.** Every model has its own cache TTL: DeepSeek ~85 min,
   Spark/GPT/Gemini ~10 min, Claude/Kimi/Qwen/MiniMax and the rest ~5 min.
   Starts at 10% before expiry (TTL × 0.9). One caveat: Hermes evicts idle
   chats after 60 min, so DeepSeek effectively fires at ~54 min
   (see How it works).
4. **Skips the small stuff.** The gate is a share of the model window
   (default 0.2, set it in the Plugins hub) — short chats are skipped,
   nothing wasted.
5. **Money is visible.** Every compaction logs SAVINGS: before → after, cost;
   on your return — whether the entry was warm or cold and what was actually
   saved.
6. **Survives restarts.** Timers persist on disk — a gateway restart never
   breaks a scheduled compaction.
7. **Windows and Linux.** One codebase: locks via `msvcrt`/`fcntl`, paths via
   `%LOCALAPPDATA%`/`~/.hermes`.

Bottom line: smaller context — cheaper every answer, no waiting when idle
compaction finishes in time, every number in the log.

## Warm-path measurements (2026-10-29, opencode-go, live requests)

| model | transport | cache_read | fresh input | cache hit |
|---|---|---|---|---|
| muse-spark-1.3-contributor | codex_responses | 1393 | 83 | 94% |
| deepseek-v4.1-flash | chat_completions | 1792 | 46 | 97.5% |
| minimax-m3.1-flash | anthropic_messages | 1920 | 153 | 92.6% |

Summaries stay structural (Goal / Current state / Next step); tools and
reasoning are unaffected.

## How it works

1. After every turn — a timer at 90% of cache lifetime: spark/gpt/gemini →
   540 s, the rest → 270 s. DeepSeek cache lives 85 min, but Hermes evicts
   the chat from memory after 60 min — so the timer fires at ~54 min.
   To use the full lifetime, raise the limit with one config block:

   ```yaml
   agent:
     agent_cache:
       idle_ttl_secs: 5400
   ```

   The timer moves to ~76 min on its own, no code changes needed.
2. On fire: session still quiet, human source, size above gate
   (gate = auto_compact_ratio × model window, default 0.2; overrides:
   Plugins-hub field, then `plugins.idle_compact.gate_fraction`, then
   2 × live `threshold_tokens × target_ratio`).
3. The plugin grabs the last real request (`pre_api_request` hook) and enables
   the warm path on its own context engine.
4. The core does the compaction (`resident._compress_context`): locks,
   cooldown, anti-thrash, watermark against races. The plugin only swaps the
   summary LLM call — all middle-replacement logic stays core.
5. Warm unavailable (no capture, route changed, error) — automatic fallback to
   the stock aux path.
6. Small/young/cron sessions are left alone.

## Custom context engine

The plugin registers `idle_warm` — a subclass of the stock `ContextCompressor`
overriding a single method, `_call_summary_llm`. Nothing in the core is
patched: plain subclassing of a public class.

If the core changes that method's signature, the plugin disables warm and keeps
working via aux (`inspect.signature` check at startup).

Requires `context.engine: idle_warm` in config:

```bash
hermes config set context.engine idle_warm
```

## Install

```bash
hermes plugins install <git-url> --enable
hermes config set context.engine idle_warm
```

Optional: auto-compaction threshold as a share of the model window (default
0.2 = twice the core target at stock 0.50/0.20) — also available as a field in
the Desktop/TUI Plugins hub:

```bash
hermes config set plugins.idle_compact.gate_fraction 0.3
```

Lean is the core default tail mode; nothing to configure.

## Rollback

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
# one-time cleanup of leftover state from ≤0.9.17
rm ~/.hermes/idle_compact_pending.json ~/.hermes/idle_*.json
```

Compaction archives rows (`active=0`), never deletes.

## Disclosures

- Network: the warm summary request goes straight to the same model
  provider/backend the session already uses. No third-party services.
- Reads: session database (state, usage counters) and the in-flight LLM
  request body (via the `llm_execution` middleware) to reuse its prefix.
- Background work: per-session timers plus a ~60s daemon sweep thread;
  state lives in `~/.hermes/idle_compact_pending.json` and `idle_*.json`.
- No credentials read or stored. No telemetry or usage reporting.

---

# idle-compact (на русском)

Фоновое сжатие простаивающих чатов Hermes-агента — до истечения prompt-cache.

## Проблема

Длинные чаты упираются в лимит контекста, а штатное сжатие бьёт трижды:
срабатывает в момент вашего запроса — вы ждёте десятки секунд; считает
**на холодную** — aux-вызов идёт мимо prompt-cache (~0% попаданий по нашим
замерам), каждое сжатие оплачивается по полному тарифу; а долгий простой
протухает кеш окончательно — следующий ход перечитывает весь контекст
по полной цене.

## Преимущества

1. **Ноль ожидания, если успело.** Сжимает заранее, в простое — если сжатие
   завершилось до вашего возвращения, ждать его не придётся.
2. **Сжатие по кешу, а не мимо.** Запрос суммирования повторяет последний ход
   и попадает в prompt-cache на 92.6–97.5% в трёх замерах вместо ~0% — само сжатие стоит копейки.
3. **Момент подбирает сам.** У каждой модели свой TTL кеша: DeepSeek ~85 мин,
   Spark/GPT/Gemini ~10 мин, Claude/Kimi/Qwen/MiniMax и остальные ~5 мин.
   Старт за 10% до протухания (TTL × 0.9). Нюанс: Hermes выгоняет чат
   из памяти через 60 мин, поэтому дипсик срабатывает на 54-й минуте
   (подробности — в Логике).
4. **Мелочь не жмёт.** Порог — доля окна модели (дефолт 0.2, меняется
   в хабе плагинов) — короткие чаты пропускаются, ничего не тратится.
5. **Деньги видны.** После каждого сжатия пишет SAVINGS: было → стало, цена;
   на вашем возврате — тёплым или холодным был вход и сколько реально
   сэкономлено.
6. **Переживает рестарт.** Таймеры сохраняются на диск — рестарт шлюза сжатие
   не срывает.
7. **Windows и Linux.** Один код: лок через `msvcrt`/`fcntl`, пути через
   `%LOCALAPPDATA%`/`~/.hermes`.

Итого: меньше контекст — дешевле каждый ответ, без ожидания, если сжатие успело на простое, каждая
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

1. После каждого хода — таймер на 90% срока жизни кеша: spark/gpt/gemini →
   540 сек, остальные → 270 сек. У дипсика кеш живёт 85 мин, но Hermes
   выгоняет чат из памяти через 60 мин — поэтому таймер срабатывает
   на 54-й минуте. Чтобы использовать полный срок, поднимите лимит
   строкой в конфиге:

   ```yaml
   agent:
     agent_cache:
       idle_ttl_secs: 5400
   ```

   Таймер сам переедет на ~76 минут, код трогать не надо.
2. Сработал: сессия тиха, источник human, размер > гейта
   (гейт = auto_compact_ratio × окно модели, дефолт 0.2; переопределения:
   поле в хабе плагинов, затем `plugins.idle_compact.gate_fraction`, затем
   2 × живой `threshold_tokens × target_ratio`).
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

Опционально: порог автосжатия как доля окна модели (дефолт 0.2 = две цели
при стоковых 0.50/0.20) — есть и полем в хабе плагинов Desktop/TUI:

```bash
hermes config set plugins.idle_compact.gate_fraction 0.3
```

Хвост lean — дефолт ядра, настраивать нечего.

## Откат

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
# разовая чистка остатков от версий ≤0.9.17
rm ~/.hermes/idle_compact_pending.json ~/.hermes/idle_*.json
```

Сжатие архивирует строки (`active=0`), не удаляет.
