# idle-compact

Cache-warm compaction for Hermes-agent chats — in the background when idle, on the warm path when busy.

## The problem

Long chats hit the context limit, and stock compression hurts three times:
it fires mid-request — you wait tens of seconds; it computes **cold** — the
aux call bypasses prompt-cache (~0% hits by our measurements), so every
compaction is billed at full rate; and a long idle lets the cache expire
completely — your next turn re-reads the whole context at full price.

## Advantages

1. **Tens of times cheaper** (measured ~20× on a 228K context): instead of paying for the conversation twice, you pay pennies — the summary reads what you already paid for.
2. **No waiting when idle.** Compaction runs while you're away; you come back to a slimmed chat and the answer starts right away.
3. **Helps with no idle time too.** Hit the threshold mid-work and compaction still runs over what you already paid for, not at full price.
4. **Picks the moment itself, with margin.** Providers keep your conversation for different periods; where it's known we fire ahead of it, where it isn't we keep a conservative margin.
5. **Leaves small chats alone.** Short chats are skipped — no extra request is sent.
6. **Money is visible.** After an idle compaction the log shows before → after and its cost; on your next turn, what it actually saved.
7. **Survives restarts.** A scheduled compaction isn't lost when the gateway restarts.

Bottom line: cheaper answers, no waiting when idle, every number in the log.


## Stock vs plugin (measured)

| | Stock compression | idle-compact |
|---|---|---|
| Your wait | ~3 min mid-answer (p50 184 s, [#117886](https://github.com/NousResearch/hermes-agent/issues/117886)) | 0 — compacts in the background (a pass takes 18–22 s off the critical path) |
| Cache use | cold: cache_read = 0 | ~99.85% from cache (241 792 cached / 356 fresh) |
| Price | ~$0.068 to cold re-read 228K tokens (prevented) | $0.00346 per compaction — **nearly 20× cheaper** |

Wait from #117886 (gateway session); cache, cost and savings from our logs:
2026-11-12 case, 228K-token session, opencode-go relay (ledger prices match
the provider dashboard).
Summaries stay structural (Goal / Current state / Next step); tools and
reasoning are unaffected.

## How it works

1. After every turn — a timer 30 s short of cache lifetime: spark/gpt/gemini →
   570 s, the rest → 270 s. DeepSeek cache lives 85 min, but Hermes evicts
   the chat from memory after 60 min — so the timer fires at ~59 min.
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

How it works: every message starts a timer — the model
cache TTL minus 30 seconds. A new message resets the countdown. Idle through the
whole timer with the context above the gate — compaction runs in
the background. You come back to an already-slim chat.

Optional: your own threshold as a share of the model window —
0.2 means "compact past 20% of the window". Lower = earlier and
more often. A set number (above zero) replaces target × 2.
Set it in the Plugins hub (→ Save), or with one command:

```bash
hermes config set plugins.idle_compact.gate_fraction 0.2
```

Works on Windows and Linux.

Check status any time with `/idle-compact` in chat (read-only):
money saved, ledger breakdown, current size vs gate, watch state.
`/idle-compact debug` shows internals (engine, timers, exact figures).
Desktop also shows a status-bar chip (`~185K left`, or `ready`
past the gate); click it for the full panel as a popup.

## Rollback

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
# one-time cleanup of leftover state from ≤0.9.17
rm ~/.hermes/idle_compact_pending.json ~/.hermes/idle_*.json
# Windows (PowerShell):
# Remove-Item "$env:LOCALAPPDATA\hermes\idle_compact_pending.json", "$env:LOCALAPPDATA\hermes\idle_*.json"
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

1. **Сжатие в десятки раз дешевле** (замер: ~20× на контексте 228K). Вместо повторной оплаты всего разговора доплачиваете копейки: сжатие читает то, за что вы уже заплатили.
2. **В простое ждать не нужно.** Сжатие идёт, пока вы отошли. Вернулись — чат уже ужат, ответ начинается сразу.
3. **Помогает и без простоя.** Порог наступил посреди работы — сжатие всё равно идёт по уже оплаченному разговору, а не по полной цене.
4. **Момент выбирает сам, с запасом.** Провайдеры держат разговор в памяти разное время; где срок известен — сжимаем заранее, где нет — берём осторожный запас.
5. **Мелочь не жмёт.** Короткие чаты пропускаются — лишнего запроса не будет.
6. **Деньги видно.** После сжатия в простое лог показывает: было → стало, сколько стоило; на следующем ходе — сколько сэкономило.
7. **Переживает рестарт.** Запланированное сжатие не теряется при перезапуске шлюза.

Итого: ответы дешевле, в простое ждать не надо, каждая цифра в логе.


## Штатное и наше (замеры)

| | Штатное сжатие | idle-compact |
|---|---|---|
| Ваше ожидание | ~3 мин посреди ответа (p50 184 с, [#117886](https://github.com/NousResearch/hermes-agent/issues/117886)) | 0 — жмёт в фоне (проход 18–22 с вне ответа) |
| Кеш | вхолодную: cache_read = 0 | ~99.85% из кеша (241 792 из кеша / 356 свежих) |
| Цена | ~$0.068 за холодный перечит 228K (предотвращён) | $0.00346 за сжатие — **почти в 20 раз дешевле** |

Ожидание — из #117886 (сессия шлюза); кеш, цена и экономия — наши логи:
кейс 12.11.2026, сессия на 228K токенов, релей opencode-go (учётные цены совпадают с дашбордом).
Саммари при этом остаётся структурным (Goal / Current state / Next step),
tools и reasoning не ломаются.

## Логика

1. После каждого хода — таймер за 30 сек до смерти кеша: spark/gpt/gemini →
   570 сек, остальные → 270 сек. У дипсика кеш живёт 85 мин, но Hermes
   выгоняет чат из памяти через 60 мин — поэтому таймер срабатывает
   на 59-й минуте. Чтобы использовать полный срок, поднимите лимит
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

Как это работает: после каждого сообщения — таймер: время жизни
кеша минус 30 секунд. Новое сообщение сбрасывает отсчёт. Досидели в тишине, контекст
больше гейта — сжатие в фоне. Вернулся — чат уже ужатый.

Опционально: свой порог как доля окна модели — 0.2 означает
«жать после 20% окна». Меньше число — раньше и чаще. Указанное
число (больше нуля) отключает цель × 2 — дальше работает оно.
Задаётся в хабе плагинов (→ Сохранить) либо одной командой:

```bash
hermes config set plugins.idle_compact.gate_fraction 0.2
```

Работает на Windows и Linux.

Состояние — командой `/idle-compact` в чате (только чтение):
сэкономленные деньги, разбивка, текущий контекст против порога, наблюдение.
`/idle-compact debug` — внутренности (движок, таймеры, точные цифры).
В десктопе — чип в статус-баре (`~185K left`, выше порога `ready`); клик —
полная панель всплывашкой.

## Откат

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
# разовая чистка остатков от версий ≤0.9.17
rm ~/.hermes/idle_compact_pending.json ~/.hermes/idle_*.json
# Windows (PowerShell):
# Remove-Item "$env:LOCALAPPDATA\hermes\idle_compact_pending.json", "$env:LOCALAPPDATA\hermes\idle_*.json"
```

Сжатие архивирует строки (`active=0`), не удаляет.

## License / Лицензия

MIT — see LICENSE file / см. файл LICENSE.
