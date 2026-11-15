/**
 * idle-compact desktop half: status-bar chip + toast panel.
 *
 * Plain ESM for the runtime pipeline (`~/.hermes/plugins/idle-compact/desktop/plugin.js`).
 * SDK surface only: statusBar chip, gateway events, in-app toast.
 *
 * Data arrives by PUSH: the Python half broadcasts `plugin.idle-compact.status`
 * after every turn. Push reaches every window, including Desktop-over-SSH
 * (`serve`), where direct RPC (`host.request`) answers -32601. RPC stays only
 * as a one-shot fallback for the click before the first event.
 * No transcript rows: the chip is always visible, the full panel shows on click
 * and dismisses by itself.
 */

import { atom, host, Tip, useValue } from '@hermes/plugin-sdk'
import { jsx } from 'react/jsx-runtime'

const RPC_METHOD = 'plugin.idle-compact.status'
const EVENT_TYPE = 'plugin.idle-compact.status'
const TOAST_MS = 12000
const TOAST_ID = 'idle-compact-status'

// Toggle state: the SDK has no dismiss verb, so hiding is done by
// re-notifying the same id with a 1ms lifetime (replaces + instantly expires).
let toastShown = false
let toastAt = 0
let rpcUnsupported = false

function currentSessionId() {
  try {
    const a = host.state && host.state.activeSessionId
    return (a && typeof a.get === 'function' ? a.get() : null) || null
  } catch (e) {
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

async function fetchStatus() {
  const params = {}
  const sid = currentSessionId()
  if (sid) {
    params.session_id = sid
  }
  const res = await host.request(RPC_METHOD, params, 15000)
  if (!res || typeof res !== 'object') {
    throw new Error('bad status payload')
  }
  return {
    chip: typeof res.chip === 'string' ? res.chip : 'idle-compact',
    panel: typeof res.panel === 'string' ? res.panel : ''
  }
}

function Chip({ data }) {
  const snap = useValue(data)

  const onClick = async () => {
    if (toastShown && Date.now() - toastAt < TOAST_MS) {
      toastShown = false
      try {
        host.notify({ durationMs: 1, id: TOAST_ID, kind: 'info', message: '', title: 'idle-compact' })
      } catch (e) {
        // Fallback: let it expire on its own.
      }
      return
    }
    if (snap.panel) {
      toastShown = true
      toastAt = Date.now()
      host.notify({
        durationMs: TOAST_MS,
        id: TOAST_ID,
        kind: 'info',
        message: snap.panel,
        onDismiss: () => { toastShown = false },
        title: 'idle-compact'
      })
      return
    }
    if (rpcUnsupported) {
      host.notify({ durationMs: TOAST_MS, kind: 'info', message: 'idle-compact status lives in chat — run /idle-compact', title: 'idle-compact' })
      return
    }
    try {
      const fresh = await fetchStatus()
      data.set(fresh)
      toastShown = true
      toastAt = Date.now()
      host.notify({
        durationMs: TOAST_MS,
        id: TOAST_ID,
        kind: 'info',
        message: fresh.panel,
        onDismiss: () => { toastShown = false },
        title: 'idle-compact'
      })
    } catch (e) {
      if (isUnknownMethod(e)) {
        rpcUnsupported = true
        host.notify({ durationMs: TOAST_MS, kind: 'info', message: 'idle-compact status lives in chat — run /idle-compact', title: 'idle-compact' })
        return
      }
      const detail = (e && (e.message || e.data || e.code)) || e
      host.notify({ durationMs: TOAST_MS, kind: 'error', message: `idle-compact: ${String(detail)}`, title: 'idle-compact' })
    }
  }

  return jsx(Tip, {
    label: 'idle-compact status — click for details',
    children: jsx('button', {
      onClick,
      className: 'inline-flex h-full cursor-pointer items-center gap-1 bg-transparent px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
      children: snap.chip
    })
  })
}

export default {
  id: 'idle-compact',
  name: 'idle-compact',
  register(ctx) {
    const data = atom({ chip: 'idle-compact', panel: '' })

    ctx.register({
      area: 'statusBar.right',
      id: 'chip',
      order: 120,
      render: () => jsx(Chip, { data })
    })

    ctx.onEvent(EVENT_TYPE, (event) => {
      try {
        const p = (event && event.payload) || {}
        if (typeof p.chip !== 'string') {
          return
        }
        if (p.session_id && p.session_id !== currentSessionId()) {
          return
        }
        data.set({
          chip: p.chip,
          panel: typeof p.panel === 'string' ? p.panel : ''
        })
      } catch (e) {
        // A bad frame must never break the chip.
      }
    })

    // One best-effort pull for the gateway route; the push covers the rest.
    // A slow or failing backend must never block chip registration.
    try {
      const out = fetchStatus()
      if (out && typeof out.then === 'function') {
        out.then(
          (fresh) => data.set(fresh),
          (e) => { if (isUnknownMethod(e)) { rpcUnsupported = true } }
        )
      }
    } catch (e) {
      // No RPC here — the push will fill the chip on the next turn.
    }
  }
}
