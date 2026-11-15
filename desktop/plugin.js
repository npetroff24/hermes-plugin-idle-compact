/**
 * idle-compact desktop half: status-bar chip + toast panel.
 *
 * Plain ESM for the runtime pipeline (`~/.hermes/plugins/idle-compact/desktop/plugin.js`).
 * SDK surface only: statusBar chip, gateway RPC to the Python half, in-app toast.
 * No transcript rows: the chip is always visible, the full panel shows on click
 * and dismisses by itself.
 */

import { atom, host, Tip, useValue } from '@hermes/plugin-sdk'
import { jsx } from 'react/jsx-runtime'

const RPC_METHOD = 'plugin.idle-compact.status'
const POLL_MS = 30000
const TOAST_MS = 12000
const TOAST_ID = 'idle-compact-status'

// Toggle state: the SDK has no dismiss verb, so hiding is done by
// re-notifying the same id with a 1ms lifetime (replaces + instantly expires).
let toastShown = false
let toastAt = 0

function currentSessionId() {
  try {
    const a = host.state && host.state.activeSessionId
    return (a && typeof a.get === 'function' ? a.get() : null) || null
  } catch (e) {
    return null
  }
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
    panel: typeof res.panel === 'string' ? res.panel : 'idle-compact: no data'
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

    let stopped = false
    const refresh = async () => {
      if (stopped) {
        return
      }
      try {
        data.set(await fetchStatus())
      } catch (e) {
        // Gateway down or Python half unloaded: keep last good text.
      }
    }

    ctx.register({
      area: 'statusBar.right',
      id: 'chip',
      order: 120,
      render: () => jsx(Chip, { data })
    })

    refresh()
    const timer = setInterval(refresh, POLL_MS)
    let unlisten = null
    try {
      const a = host.state && host.state.activeSessionId
      if (a && typeof a.listen === 'function') {
        unlisten = a.listen(refresh)
      }
    } catch (e) {
      unlisten = null
    }
    ctx.onDispose(() => {
      stopped = true
      clearInterval(timer)
      if (typeof unlisten === 'function') {
        unlisten()
      }
    })
  }
}
