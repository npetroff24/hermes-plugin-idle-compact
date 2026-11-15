# idle-compact

Cache-warm compaction for Hermes-agent chats — in the background when idle, on the warm path when busy.

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
   and hits prompt-cache at ~99.8% instead of ~0% — about **5.4× cheaper**
   (see Stock vs plugin).
3. **Timing per model.** Fires at 10% before cache expiry (TTL × 0.9).
   Measured on our traffic: DeepSeek ~85 min, Spark ~10 min. Documented
   by vendors: Claude, Kimi — 5 min. The rest is a conservative default
   (5 min; GPT/Gemini 10 min). One caveat: Hermes evicts idle
   chats after 60 min, so DeepSeek effectively fires at ~54 min
   (see How it works).
4. **Skips the small stuff.** The gate is a share of the model window
   (suggested 0.2 in the Plugins hub; until you save, core target × 2) —
   short chats are skipped,
   nothing wasted.
5. **Money is visible.** Every compaction logs SAVINGS: before → after, cost;
   on your return — whether the entry was warm or cold and what was actually
   saved.
6. **Survives restarts.** Timers persist on disk — a gateway restart never
   breaks a scheduled compaction.
7. **Windows and Linux.** One codebase: locks via `msvcrt`/`fcntl`, paths via
   `%LOCALAPPDATA%`/`~/.hermes`.
8. **Warm everywhere, not just when idle.** The engine holds your latest
   request, so compression that fires mid-work — threshold hit with no idle
   time — takes the same warm path. No fresh capture — falls back to the
   stock aux call (see How it works).

Bottom line: smaller context — cheaper every answer, no waiting when idle
compaction finishes in time, every number in the log.

## Stock vs plugin (measured)

| | Stock compression | idle-compact |
|---|---|---|
| Your wait | ~3 min mid-answer (p50 184 s, [#117886](https://github.com/NousResearch/hermes-agent/issues/117886)) | 0 — compacts in the background (a pass takes 18–22 s off the critical path) |
| Cache use | cold: cache_read = 0 | ~99.8% from cache (130 432 cached / 280 fresh) |
| Price | $0.0188 per ~60K tokens | $0.0035 per ~64K tokens — **≈5.4× cheaper** |

Wait from #117886 (gateway session); cache and prices from our provider
dashboard and logs, 2026-11-13, opencode-go relay.
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
2. On fire: session still quiet, source not machine, size above gate.
   The gate is your saved threshold (a share of the model window),
   or the core compression target × 2 when nothing is saved.
3. The plugin grabs the last real request (`pre_api_request` hook) and enables
   the warm path on its own context engine.
4. The core does the compaction (its own `_compress_context`): locks,
   cooldown, anti-thrash, watermark against races. The plugin only swaps the
   summary LLM call — all middle-replacement logic stays core.
5. Warm unavailable (no capture, route changed, error) — automatic fallback to
   the stock aux path.
6. Small sessions and machine sessions (cron, subagents, oneshot…) are left alone.

## Custom context engine

The plugin registers `idle_warm` — a subclass of the stock `ContextCompressor`
overriding a single method, `_call_summary_llm`. Nothing in the core is
patched — plain subclassing — but that override point is not a public API
yet; the catalog listing waits on the core seam (#97390).

If the core changes that method's signature, the plugin disables warm and keeps
working via aux (`inspect.signature` check at startup).

Requires `context.engine: idle_warm` in config:

```bash
hermes config set context.engine idle_warm
```

## Install

```bash
hermes plugins install https://github.com/npetroff24/hermes-plugin-idle-compact --enable
hermes config set context.engine idle_warm
```

By default, auto-compression kicks in once the context passes the
core compression target × 2.

How it works: every message starts a timer — 90% of the model
cache TTL. A new message resets the countdown. Idle through the
whole timer with the context above the gate — compaction runs in
the background. You come back to an already-slim chat.

Optional: your own threshold as a share of the model window —
0.2 means "compact past 20% of the window". Lower = earlier and
more often. A set number (above zero) replaces target × 2.
Set it in the Plugins hub (→ Save), or with one command:

```bash
hermes config set plugins.idle_compact.gate_fraction 0.2
```

## Rollback

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
# one-time cleanup of leftover state from ≤0.9.17
rm ~/.hermes/idle_compact_pending.json ~/.hermes/idle_*.json
# Windows (PowerShell):
# Remove-Item "$env:LOCALAPPDATA\\hermes\\idle_compact_pending.json", "$env:LOCALAPPDATA\\hermes\\idle_*.json"
```

Compaction archives rows (`active=0`), never deletes.

## Disclosures

- Network: the warm summary request goes straight to the same model
  provider/backend the session already uses. No third-party services.
- Transport: the warm request goes through `httpx` and identifies itself as
  `idle-compact/<version>` from `plugin.yaml`. The warm call cannot borrow the
  core's client, so the plugin signs its own requests; a bare/default client
  signature is rejected by Cloudflare-fronted relays with
  `HTTP 403 error code 1010`, and the warm path then falls back to the cold
  aux call.
- Reads: session database (state, usage counters) and the in-flight LLM
  request body (via the `llm_execution` middleware) to reuse its prefix.
- Credentials: reads the agent's API key in-process for the warm call;
  never stored, logged, or sent anywhere except the session's own provider.
- Background work: per-session timers plus a ~30s daemon sweep thread;
  state lives in `idle_compact_pending.json` and `idle_*.json` under the
  Hermes home directory (`%LOCALAPPDATA%\hermes` on Windows, `~/.hermes` elsewhere).
- Redirects: the warm request never follows them — any 3xx is refused, so the
  provider key can't leak to another host on a cross-host redirect.
- No telemetry or usage reporting.

---

# idle-compact (на русском)

Сжатие чатов Hermes-агента по кешу — в фоне, когда простой, тёплым путём, когда работа.

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
   и попадает в prompt-cache на ~99.8% вместо ~0% — примерно **в 5.4 раза дешевле**
   (см. Штатное и наше).
3. **Момент подбирает сам.** Старт за 10% до протухания кеша (TTL × 0.9).
   Замерено на нашем трафике: DeepSeek ~85 мин, Spark ~10 мин.
   Документировано вендорами: Claude, Kimi — 5 мин. Остальное —
   консервативный дефолт (5 мин; GPT/Gemini 10 мин). Нюанс: Hermes выгоняет чат
   из памяти через 60 мин, поэтому дипсик срабатывает на 54-й минуте
   (подробности — в Логике).
4. **Мелочь не жмёт.** Порог — доля окна модели (в хабе плагинов
   подсказано 0.2; пока не сохранили — цель ядра × 2) — короткие чаты
   пропускаются, ничего не тратится.
5. **Деньги видны.** После каждого сжатия пишет SAVINGS: было → стало, цена;
   на вашем возврате — тёплым или холодным был вход и сколько реально
   сэкономлено.
6. **Переживает рестарт.** Таймеры сохраняются на диск — рестарт шлюза сжатие
   не срывает.
7. **Windows и Linux.** Один код: лок через `msvcrt`/`fcntl`, пути через
   `%LOCALAPPDATA%`/`~/.hermes`.
8. **Тепло везде, а не только в простое.** Движок держит последний запрос,
   поэтому сжатие, сработавшее прямо во время работы — порог достигнут
   без простоя — идёт тем же тёплым путём. Свежего захвата нет — откат
   на штатный aux-вызов (см. Логику).

Итого: меньше контекст — дешевле каждый ответ, без ожидания, если сжатие успело на простое, каждая
цифра в логе.

## Штатное и наше (замеры)

| | Штатное сжатие | idle-compact |
|---|---|---|
| Ваше ожидание | ~3 мин посреди ответа (p50 184 с, [#117886](https://github.com/NousResearch/hermes-agent/issues/117886)) | 0 — жмёт в фоне (проход 18–22 с вне ответа) |
| Кеш | вхолодную: cache_read = 0 | ~99.8% из кеша (130 432 из кеша / 280 свежих) |
| Цена | $0.0188 за ~60K токенов | $0.0035 за ~64K токенов — **примерно в 5.4 раза дешевле** |

Ожидание — из #117886 (сессия шлюза); кеш и цены — наш дашборд
провайдера и логи, 13.11.2026, релей opencode-go.
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
2. Сработал: сессия тиха, источник не машинный, размер > гейта.
   Гейт — сохранённый порог (доля окна модели) либо цель
   сжатия ядра × 2, если ничего не сохранено.
3. Плагин захватывает последний реальный запрос (хук `pre_api_request`) и
   включает warm-путь на своём контекст-движке.
4. Сжатие делает ядро (свой `_compress_context`): локи, cooldown,
   anti-thrash, watermark против гонок. Плагин подменяет только LLM-вызов
   суммирования — вся логика замены середины остаётся ядровой.
5. Warm недоступен (нет захвата, сменился маршрут, ошибка) — автоматический
   откат на штатный aux-путь.
6. Маленькие сессии и машинные (cron, субагенты, oneshot…) не трогаются.

## Свой контекст-движок

Плагин регистрирует `idle_warm` — наследник штатного `ContextCompressor`,
который переопределяет единственный метод `_call_summary_llm`. Ничего в ядре
не патчится — обычное наследование, — но эта точка переопределения пока
не публичный API; листинг в каталоге ждёт шов в ядре (#97390).

Если ядро сменит сигнатуру этого метода, плагин отключит warm и продолжит
работать через aux (проверка `inspect.signature` на старте).

Нужен `context.engine: idle_warm` в конфиге:

```bash
hermes config set context.engine idle_warm
```

## Установка

```bash
hermes plugins install https://github.com/npetroff24/hermes-plugin-idle-compact --enable
hermes config set context.engine idle_warm
```

По умолчанию автокомпрессия активируется когда контекст переваливает
за цель сжатия ядра × 2.

Как это работает: после каждого сообщения — таймер 90% времени жизни
кеша. Новое сообщение сбрасывает отсчёт. Досидели в тишине, контекст
больше гейта — сжатие в фоне. Вернулся — чат уже ужатый.

Опционально: свой порог как доля окна модели — 0.2 означает
«жать после 20% окна». Меньше число — раньше и чаще. Указанное
число (больше нуля) отключает цель × 2 — дальше работает оно.
Задаётся в хабе плагинов (→ Сохранить) либо одной командой:

```bash
hermes config set plugins.idle_compact.gate_fraction 0.2
```

## Откат

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
# разовая чистка остатков от версий ≤0.9.17
rm ~/.hermes/idle_compact_pending.json ~/.hermes/idle_*.json
# Windows (PowerShell):
# Remove-Item "$env:LOCALAPPDATA\\hermes\\idle_compact_pending.json", "$env:LOCALAPPDATA\\hermes\\idle_*.json"
```

Сжатие архивирует строки (`active=0`), не удаляет.
