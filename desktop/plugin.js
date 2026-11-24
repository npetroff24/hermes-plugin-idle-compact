/**
 * idle-compact desktop half: status-bar chip + click panel.
 *
 * Plain ESM for the runtime pipeline (`~/.hermes/plugins/idle-compact/desktop/plugin.js`).
 * SDK surface only: statusBar chip, Popover panel, gateway events.
 *
 * The chip is a glyph plus the savings percentage, not the token remainder:
 * the percentage is the number this plugin exists to move, and the remainder
 * needs a sentence next to it, which is what the panel is for. Both come
 * counted from the Python half (`savings` / `chat` / `bar`); this file draws
 * them and never derives a figure of its own.
 *
 * Data arrives by PUSH: the Python half broadcasts `plugin.idle-compact.status`
 * after every turn. Push reaches every window, including Desktop-over-SSH
 * (`serve`), where direct RPC (`host.request`) answers -32601. RPC stays as the
 * refresh on click, where it can afford the exact core count.
 */

import {
  atom,
  Codicon,
  host,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Separator,
  Tip,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx } from 'react/jsx-runtime'

const RPC_METHOD = 'plugin.idle-compact.status'
const EVENT_TYPE = 'plugin.idle-compact.status'
const TOAST_MS = 12000

// A trend line reads as "rate" beside a percentage, and does not compete with
// the money and connection glyphs already in the bar. The word is spoken by
// the tooltip and the panel.
const CHIP_GLYPH = 'graph'

// Mirrors STATUSBAR_ACTION_CLASS in the app's statusbar-controls.tsx: the
// app does not export it, and without the same utilities the chip does not
// hover or fade like the items either side of it.
const CHIP_CLASS =
  'inline-flex h-full cursor-pointer items-center gap-1 rounded-none px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground'

const EMPTY = { chip: '', panel: '', bar: null, savings: null, chat: null, version: '' }

let rpcUnsupported = false
let lastPushTs = 0

function currentSessionId() {
  try {
    const a = host.state && host.state.activeSessionId
    return (a && typeof a.get === 'function' ? a.get() : null) || null
  } catch {
    return null
  }
}

function focusedSessionId() {
  try {
    const f = host.state && host.state.focusedSessionId
    return (f && typeof f.get === 'function' ? f.get() : null) || null
  } catch {
    return null
  }
}

function storedSessionId() {
  try {
    const s = host.state && host.state.focusedStoredSessionId
    return (s && typeof s.get === 'function' ? s.get() : null) || null
  } catch {
    return null
  }
}

function isUnknownMethod(e) {
  if (!e) {
    return false
  }
  if (e.code === -32601 || e.data === -32601) {
    return true
  }
  const text = String((e && e.message) || e)
  return text.indexOf('unknown method') >= 0 || text.indexOf('-32601') >= 0
}

/** One shape for both doors: a push payload and an RPC answer look the same. */
function snapshotOf(payload) {
  const p = payload && typeof payload === 'object' ? payload : {}
  return {
    chip: typeof p.chip === 'string' ? p.chip : '',
    panel: typeof p.panel === 'string' ? p.panel : '',
    bar: p.bar && typeof p.bar === 'object' ? p.bar : null,
    savings: p.savings && typeof p.savings === 'object' ? p.savings : null,
    chat: p.chat && typeof p.chat === 'object' ? p.chat : null,
    version: typeof p.version === 'string' ? p.version : ''
  }
}

async function fetchStatus() {
  const params = {}
  // RPC-путь (_resident_desktop) ищет по агентскому ключу, а не по runtime id.
  const sid = storedSessionId() || focusedSessionId() || currentSessionId()
  if (sid) {
    params.session_id = sid
  }
  const res = await host.request(RPC_METHOD, params, 15000)
  if (!res || typeof res !== 'object') {
    throw new Error('bad status payload')
  }
  return snapshotOf(res)
}

function matchSession(p) {
  if (!p || (!p.session_id && !p.session_key && !p.agent_session_id)) {
    return true
  }
  try {
    const ids = []
    const a = host.state && host.state.activeSessionId
    if (a && typeof a.get === 'function' && a.get()) {
      ids.push(a.get())
    }
    const f = host.state && host.state.focusedSessionId
    if (f && typeof f.get === 'function' && f.get()) {
      ids.push(f.get())
    }
    const s = host.state && host.state.focusedStoredSessionId
    if (s && typeof s.get === 'function' && s.get()) {
      ids.push(s.get())
    }
    if (ids.length === 0) {
      // Окно без активной сессии: чужой пуш не принимаем, иначе цифры
      // другой сессии выдадут себя за текущие.
      return false
    }
    return [p.session_id, p.session_key, p.agent_session_id].some(
      (v) => typeof v === 'string' && v && ids.indexOf(v) >= 0
    )
  } catch {
    return true
  }
}

/** The savings percentage, or null when there is no baseline to divide by. */
function savingsPct(snap) {
  const s = snap && snap.savings
  if (!s || s.pct === null || s.pct === undefined) {
    return null
  }
  const pct = Number(s.pct)
  return Number.isFinite(pct) ? Math.round(pct) : null
}

function fmtT(n) {
  // Коротко, как Python _fmt_k: 42245 → «~42K», 850 → «~850».
  const v = Number(n)
  if (!Number.isFinite(v) || v < 0) {
    return '?'
  }
  if (v < 1000) {
    return `~${Math.round(v)}`
  }
  return `~${Math.round(v / 1000)}K`
}

function fmtUsd(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) {
    return ''
  }
  return `${n < 0 ? '-' : '+'}$${Math.abs(n).toFixed(2)}`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function fmtWhen(ts) {
  // A three-letter month keeps the day and month unambiguous on every locale
  // the app runs in; Python's `_fmt_ts` writes 15.11, which reads as a decimal.
  const v = Number(ts)
  if (!Number.isFinite(v) || v <= 0) {
    return ''
  }
  const d = new Date(v * 1000)
  const pad = (x) => String(x).padStart(2, '0')
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function chipLabel(snap) {
  const pct = savingsPct(snap)
  const state = snap && snap.chat && snap.chat.label ? String(snap.chat.label) : ''
  const head = pct === null ? 'idle-compact' : `idle-compact · ${pct}% saved`
  return state ? `${head} · ${state}` : head
}

function Bar({ bar, width }) {
  // Шкала 0 → порог ядра: сплошное до текущего, слабая зона до гейта
  // простоя, прозрачное до конца. Цвета — от текущей темы.
  const used = Number(bar.used) || 0
  const gate = Number(bar.idle_gate) || 0
  const max = Number(bar.threshold) || 0
  if (!(max > 0) || used < 0) {
    return null
  }
  const u = Math.min(1, used / max)
  const g = Math.min(1, gate / max)
  const mix = (pct) => `color-mix(in srgb, currentColor ${pct}%, transparent)`
  return jsx('span', {
    style: {
      position: 'relative', display: 'block', width: width || 64, height: 10,
      borderRadius: 2, outline: `1px solid ${mix(35)}`, background: mix(8),
      overflow: 'hidden', flex: 'none'
    },
    children: [
      u > 0 ? jsx('span', {
        key: 'f',
        style: { position: 'absolute', left: 0, top: 0, bottom: 0, width: `${u * 100}%`, background: mix(80) }
      }) : null,
      g > u ? jsx('span', {
        key: 'z',
        style: { position: 'absolute', left: `${u * 100}%`, top: 0, bottom: 0, width: `${(g - u) * 100}%`, background: mix(30) }
      }) : null,
      (g > 0 && g < 1) ? jsx('span', {
        key: 't',
        style: { position: 'absolute', left: `calc(${g * 100}% - 1px)`, top: 0, bottom: 0, width: 1, background: 'currentColor' }
      }) : null
    ]
  })
}

/** One fact per line: a fixed label column keeps the values aligned. */
function Row({ label, children }) {
  return jsx('div', {
    style: {
      display: 'grid', gridTemplateColumns: '82px minmax(0, 1fr)', columnGap: 12,
      alignItems: 'baseline', padding: '3px 0'
    },
    children: [
      jsx('span', {
        key: 'l',
        style: { color: 'var(--ui-text-tertiary)', fontSize: '0.6875rem', lineHeight: 1.35 },
        children: label
      }),
      // The value slot is a div, not a span: a row may carry a block child
      // (the scale, a caption) and a span may not contain one.
      jsx('div', {
        key: 'v',
        style: {
          minWidth: 0, fontSize: '0.75rem', lineHeight: 1.35,
          color: 'var(--ui-text-primary)', fontVariantNumeric: 'tabular-nums'
        },
        children
      })
    ]
  })
}

function Caption({ children }) {
  return jsx('div', {
    style: {
      marginTop: 4, fontSize: '0.625rem', lineHeight: 1.35,
      color: 'var(--ui-text-tertiary)', fontVariantNumeric: 'tabular-nums'
    },
    children
  })
}

/** The headline: what the plugin has saved, counted by the Python half. */
function Hero({ snap }) {
  const savings = snap.savings
  const pct = savingsPct(snap)
  if (!savings) {
    return jsx('div', {
      style: { padding: '2px 0 10px' },
      children: [
        jsx('div', {
          key: 'v',
          style: { fontSize: '0.75rem', color: 'var(--ui-text-secondary)' },
          children: 'No compactions yet'
        }),
        jsx(Caption, { key: 'c', children: 'Savings appear after the first idle compaction.' })
      ]
    })
  }
  const net = Number(savings.net) || 0
  const base = Number(savings.base) || 0
  const caption = base > 0
    ? `${fmtUsd(net)} of $${Math.abs(base).toFixed(2)} without the plugin`
    : `${fmtUsd(net)} saved so far`
  // The ledger sits on the headline's own line, right aligned and on the
  // baseline of the "saved" word. As a second line under a 22px figure it read
  // as a second headline and pushed the scale down the panel.
  return jsx('div', {
    style: { padding: '2px 0 10px' },
    children: jsx('div', {
      key: 'v',
      style: { display: 'flex', alignItems: 'baseline', gap: 6 },
      children: [
        jsx('span', {
          key: 'p',
          style: {
            fontSize: '1.375rem', fontWeight: 600, lineHeight: 1.1,
            letterSpacing: '-0.02em', fontVariantNumeric: 'tabular-nums',
            color: 'var(--ui-text-primary)'
          },
          children: pct === null ? fmtUsd(net) : `${pct}%`
        }),
        jsx('span', {
          key: 'w',
          style: { fontSize: '0.75rem', color: 'var(--ui-text-tertiary)' },
          children: 'saved'
        }),
        jsx('span', {
          key: 'c',
          style: {
            marginLeft: 'auto', minWidth: 0, textAlign: 'right',
            fontSize: '0.625rem', lineHeight: 1.35,
            color: 'var(--ui-text-tertiary)', fontVariantNumeric: 'tabular-nums'
          },
          children: caption
        })
      ]
    })
  })
}

/** This chat's state, the context scale, and the ledger breakdown. */
function Facts({ snap }) {
  const chat = snap.chat || {}
  const bar = snap.bar
  const savings = snap.savings
  const rows = []

  rows.push(jsx(Row, {
    key: 'chat',
    label: 'This chat',
    children: String(chat.label || 'No measurement yet')
  }, 'chat'))

  if (bar) {
    // The scale is 0 → the core threshold, with the idle gate marked on it, so
    // the caption names both reference points instead of leaving them in the
    // payload: the number the reader came for is the one on the row.
    rows.push(jsx(Row, {
      key: 'context',
      label: 'Context',
      children: [
        jsx('span', { key: 'v', children: `${fmtT(bar.used)} tokens now` }),
        jsx('div', { key: 'b', style: { marginTop: 6, color: 'var(--ui-text-secondary)' }, children: jsx(Bar, { bar, width: '100%' }) }),
        // Two lines, one reference point each, so neither wraps mid-phrase.
        jsx(Caption, {
          key: 'c',
          children: [
            jsx('span', { key: 'g', children: `idle gate ${fmtT(bar.idle_gate)}` }),
            jsx('br', { key: 'br' }),
            jsx('span', { key: 't', children: `Hermes compacts at ${fmtT(bar.threshold)}` })
          ]
        })
      ]
    }, 'context'))
  }

  if (savings) {
    const parts = [`${Number(savings.paid) || 0} paid off`, `${Number(savings.wasted) || 0} wasted`]
    if (Number(savings.uncounted) > 0) {
      parts.push(`${Number(savings.uncounted)} uncounted`)
    }
    rows.push(jsx(Row, {
      key: 'count',
      label: 'Compactions',
      children: [
        jsx('span', { key: 'c', children: parts.join(' · ') }),
        // "Wasted" is the plugin's own term for a compaction the user came
        // back from before the cache could expire. It costs money, so it gets
        // one plain sentence rather than a defence.
        Number(savings.wasted) > 0
          ? jsx(Caption, { key: 'g', children: 'wasted = returned before the cache expired' })
          : null
      ]
    }, 'count'))
    const when = fmtWhen(savings.last_ts)
    if (when) {
      rows.push(jsx(Row, { key: 'last', label: 'Last', children: when }, 'last'))
    }
  }

  return jsx('div', { children: rows })
}

function Panel({ snap }) {
  const version = snap.version && snap.version !== '?' ? snap.version : ''
  return jsx('div', {
    style: { color: 'var(--ui-text-primary)' },
    children: [
      jsx('div', {
        key: 'head',
        style: { display: 'flex', alignItems: 'center', gap: 6, padding: '2px 2px 8px' },
        children: [
          jsx(Codicon, {
            key: 'g',
            name: CHIP_GLYPH,
            size: '0.75rem',
            style: { color: 'var(--ui-text-tertiary)' }
          }),
          jsx('span', {
            key: 'n',
            style: { fontSize: '0.75rem', fontWeight: 600, letterSpacing: '-0.01em' },
            children: 'idle-compact'
          }),
          jsx('span', {
            key: 'v',
            style: {
              marginLeft: 'auto', fontSize: '0.6875rem', color: 'var(--ui-text-tertiary)',
              fontVariantNumeric: 'tabular-nums'
            },
            children: version
          })
        ]
      }),
      jsx(Separator, { key: 'sep1', style: { margin: '0 0 10px' } }),
      jsx(Hero, { key: 'hero', snap }),
      jsx(Separator, { key: 'sep2', style: { margin: '0 0 8px' } }),
      jsx(Facts, { key: 'facts', snap })
    ]
  })
}

function Chip({ data, refresh }) {
  const snap = useValue(data) || EMPTY
  const [open, setOpen] = useState(false)
  const pct = savingsPct(snap)
  const label = chipLabel(snap)
  // While its panel is open the chip stays lit: a status-bar item with a
  // popover above it should read as the one that owns it.
  const openStyle = open
    ? { background: 'var(--chrome-action-hover)', color: 'var(--ui-text-primary)' }
    : null

  return jsx(Popover, {
    onOpenChange: (next) => {
      setOpen(next)
      // Refresh lives in register(): it can only decide whether its answer is
      // still current where the accepted-update generation is visible.
      if (next && refresh) {
        refresh()
      }
    },
    children: [
      jsx(Tip, {
        key: 'tip',
        label,
        children: jsx(PopoverTrigger, {
          asChild: true,
          children: jsx('button', {
            type: 'button',
            'aria-label': label,
            className: CHIP_CLASS,
            style: { fontVariantNumeric: 'tabular-nums', ...openStyle },
            children: [
              jsx(Codicon, { key: 'g', name: CHIP_GLYPH, size: '0.7rem' }),
              pct === null ? null : jsx('span', { key: 'p', children: `${pct}%` })
            ]
          })
        })
      }),
      jsx(PopoverContent, {
        key: 'panel',
        align: 'end',
        className: 'w-80',
        side: 'top',
        sideOffset: 8,
        children: jsx(Panel, { snap })
      })
    ]
  })
}

export default {
  id: 'idle-compact',
  name: 'idle-compact',
  register(ctx) {
    const data = atom(EMPTY)
    // Generation of the last update accepted into `data`: bumped by every
    // push, every applied pull and the chat-switch reset. An RPC or pull
    // answer that started before a newer update is stale even in the same
    // chat, so it must never replace that newer update.
    let generation = 0
    const accept = (snap) => {
      generation += 1
      data.set(snap)
    }
    const currentSid = () => storedSessionId() || focusedSessionId() || currentSessionId()

    // The click is the one moment an exact core count is cheap enough to ask
    // for; a failure here leaves the pushed numbers on screen.
    const refresh = () => {
      if (rpcUnsupported) {
        return
      }
      // Capture what was on screen before asking: a panel opened in chat A
      // and a chat switch (or a push) during the request leave A's snapshot
      // older than the screen, so it is dropped instead of installed.
      const wantSid = currentSid()
      const wantGen = generation
      fetchStatus().then(
        (fresh) => {
          if (generation !== wantGen || currentSid() !== wantSid) {
            return
          }
          accept(fresh)
        },
        (e) => {
          if (isUnknownMethod(e)) {
            rpcUnsupported = true
            return
          }
          const snap = (data.get && data.get()) || EMPTY
          if (snap.savings) {
            return
          }
          const detail = (e && (e.message || e.data || e.code)) || e
          try {
            host.notify({
              durationMs: TOAST_MS,
              kind: 'error',
              title: 'idle-compact',
              message: `status unavailable: ${String(detail)}`
            })
          } catch {
            // No notifications in this host — the tooltip still carries the state.
          }
        }
      )
    }

    ctx.register({
      area: 'statusBar.right',
      id: 'chip',
      order: 120,
      render: () => jsx(Chip, { data, refresh })
    })

    ctx.onEvent(EVENT_TYPE, (event) => {
      try {
        const p = (event && event.payload) || {}
        if (typeof p.chip !== 'string') {
          return
        }
        if (!matchSession(p)) {
          return
        }
        // Старый пуш поверх свежего не кладем (переупорядочивание событий).
        const ts = Number(p.ts) || 0
        if (ts < lastPushTs) {
          return
        }
        lastPushTs = ts
        accept(snapshotOf(p))
      } catch {
        // A bad frame must never break the chip.
      }
    })

    // One best-effort pull for the gateway route; the push covers the rest.
    // A slow or failing backend must never block chip registration.
    const pullOnce = () => {
      try {
        // Ответ, прилетевший после нового переключения или более свежего
        // пуша, выбрасываем: он описывает уже снятое со экрана состояние.
        const want = currentSid()
        const wantGen = generation
        const out = fetchStatus()
        if (out && typeof out.then === 'function') {
          out.then(
            (fresh) => {
              if (generation !== wantGen || currentSid() !== want) {
                return
              }
              accept(fresh)
            },
            (e) => { if (isUnknownMethod(e)) { rpcUnsupported = true } }
          )
        }
      } catch {
        // No RPC here — the push will fill the chip on the next turn.
      }
    }
    pullOnce()
    // Switching chats drops the session-scoped figures of the chat you left.
    // The savings percentage is a lifetime figure, so it stays on screen.
    const onSessionChange = () => {
      rpcUnsupported = false
      lastPushTs = 0
      const prev = (data.get && data.get()) || EMPTY
      // accept(), не data.set(): сброс обязан сдвинуть генерацию, иначе
      // ответ по покинутому чату примется как свежий.
      accept({ ...prev, chip: '', panel: '', bar: null, chat: null })
      pullOnce()
    }
    try {
      const st = host.state || {}
      const atoms = [st.activeSessionId, st.focusedSessionId, st.focusedStoredSessionId]
      let wired = 0
      for (const a of atoms) {
        if (a && typeof a.listen === 'function') {
          ctx.onDispose(a.listen(onSessionChange))
          wired++
        }
      }
      if (wired === 0) {
        throw new Error('no session atoms')
      }
    } catch {
      // No session tracking — the push still fills the chip.
    }
  }
}
