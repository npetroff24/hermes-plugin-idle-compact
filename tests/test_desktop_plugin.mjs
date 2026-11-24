/**
 * Tests for the desktop half: stale-response guards on the chip data atom.
 *
 * The plugin module imports the Hermes plugin SDK and React, which do not exist
 * in this repository, so the test writes stub modules into a temporary tree,
 * copies desktop/plugin.js beside them, and imports the real file. Nothing in
 * desktop/ is modified.
 *
 * Run: node tests/test_desktop_plugin.mjs
 */
import { strict as assert } from 'node:assert'
import { mkdtemp, mkdir, copyFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(HERE, '..', 'desktop', 'plugin.js')

const SDK = `
export const atom = (initial) => {
  let value = initial
  return {
    get: () => value,
    set: (next) => { value = next },
    listen: () => () => {}
  }
}
export const useValue = (a) => a.get()
export const host = globalThis.__host
export const Codicon = 'Codicon'
export const Popover = 'Popover'
export const PopoverContent = 'PopoverContent'
export const PopoverTrigger = 'PopoverTrigger'
export const Separator = 'Separator'
export const Tip = 'Tip'
`

const REACT = `
export const useState = (initial) => [initial, () => {}]
`

const JSX = `
export const jsx = (type, props) => ({ type, props })
export const jsxs = jsx
`

async function loadPlugin() {
  const root = await mkdtemp(join(tmpdir(), 'idle-compact-js-'))
  const desktop = join(root, 'desktop')
  await mkdir(desktop, { recursive: true })
  const sdk = join(root, 'node_modules', '@hermes', 'plugin-sdk')
  const react = join(root, 'node_modules', 'react')
  await mkdir(sdk, { recursive: true })
  await mkdir(react, { recursive: true })
  await writeFile(join(sdk, 'package.json'), JSON.stringify({ name: '@hermes/plugin-sdk', type: 'module', main: 'index.js' }))
  await writeFile(join(sdk, 'index.js'), SDK)
  await writeFile(join(react, 'package.json'), JSON.stringify({
    name: 'react',
    type: 'module',
    main: 'index.js',
    exports: { '.': './index.js', './jsx-runtime': './jsx-runtime.js' }
  }))
  await writeFile(join(react, 'index.js'), REACT)
  await writeFile(join(react, 'jsx-runtime.js'), JSX)
  await copyFile(PLUGIN, join(desktop, 'plugin.js'))
  const mod = await import(pathToFileURL(join(desktop, 'plugin.js')).href)
  return { mod, cleanup: () => rm(root, { recursive: true, force: true }) }
}

/** A promise whose settlement the test controls. */
function deferred() {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}

function makeHost(state, requestQueue) {
  return {
    state,
    request: () => {
      const next = requestQueue.shift()
      return next ? next.promise : Promise.resolve({ chip: 'pull-default' })
    },
    notify: () => {}
  }
}

function sessionAtom(initial) {
  let value = initial
  const listeners = []
  return {
    get: () => value,
    set: (next) => { value = next; listeners.forEach((fn) => fn(next)) },
    listen: (fn) => { listeners.push(fn); return () => {} }
  }
}

async function boot(state, requestQueue) {
  // The SDK stub captures globalThis.__host at import time, so it must be
  // installed before the plugin module is loaded.
  globalThis.__host = makeHost(state, requestQueue)
  const { mod, cleanup } = await loadPlugin()
  const registered = []
  const events = []
  const ctx = {
    register: (def) => registered.push(def),
    onEvent: (_type, fn) => events.push(fn),
    onDispose: () => {}
  }
  mod.default.register(ctx)
  // The registration-time best-effort pull must settle before a test starts.
  await new Promise((r) => setTimeout(r, 0))
  const rendered = registered[0].render()
  return {
    chip: rendered.props,
    push: (payload) => events.forEach((fn) => fn({ payload })),
    cleanup
  }
}

const tests = []
const test = (name, fn) => tests.push({ name, fn })

test('click refresh drops an answer for the chat you left', async () => {
  const active = sessionAtom('chat-a')
  const state = { activeSessionId: active, focusedSessionId: null, focusedStoredSessionId: null }
  const { chip, cleanup } = await boot(state, [])
  try {
    // Panel opens in chat A: the request is now in flight.
    const pending = []
    globalThis.__host.request = () => {
      const d = deferred()
      pending.push(d)
      return d.promise
    }
    chip.refresh()
    active.set('chat-b')            // the user switches chat before the answer
    pending[0].resolve({ chip: 'from-chat-a', panel: '', ts: 50 })
    await new Promise((r) => setTimeout(r, 0))
    assert.notEqual(chip.data.get().chip, 'from-chat-a',
      'a stale snapshot from chat A must not overwrite chat B')
  } finally {
    await cleanup()
  }
})

test('late pull answer does not replace a newer push', async () => {
  const active = sessionAtom('chat-a')
  const state = { activeSessionId: active, focusedSessionId: null, focusedStoredSessionId: null }
  const held = deferred()
  const { chip, push, cleanup } = await boot(state, [{ promise: held.promise }])
  try {
    // The registration-time pull started before this push arrived.
    push({ chip: 'pushed', panel: '', ts: 100 })
    held.resolve({ chip: 'pulled-older', panel: '', ts: 10 })
    await new Promise((r) => setTimeout(r, 0))
    assert.equal(chip.data.get().chip, 'pushed',
      'an RPC answer older than an accepted push must be discarded')
  } finally {
    await cleanup()
  }
})

test('chat switch invalidates an in-flight pull', async () => {
  const active = sessionAtom('chat-a')
  const state = { activeSessionId: active, focusedSessionId: null, focusedStoredSessionId: null }
  const held = deferred()
  const { chip, cleanup } = await boot(state, [{ promise: held.promise }])
  try {
    active.set('chat-b')            // switch while chat A's pull is open
    held.resolve({ chip: 'from-chat-a', panel: '', ts: 5 })
    await new Promise((r) => setTimeout(r, 0))
    assert.notEqual(chip.data.get().chip, 'from-chat-a',
      'the chat switch must invalidate the pull opened in chat A')
  } finally {
    await cleanup()
  }
})

test('a refresh inside one chat still applies', async () => {
  const active = sessionAtom('chat-a')
  const state = { activeSessionId: active, focusedSessionId: null, focusedStoredSessionId: null }
  let held
  const { chip, cleanup } = await boot(state, [])
  try {
    globalThis.__host.request = () => {
      held = deferred()
      return held.promise
    }
    chip.refresh()
    held.resolve({ chip: 'fresh', panel: '', ts: 60 })
    await new Promise((r) => setTimeout(r, 0))
    assert.equal(chip.data.get().chip, 'fresh', 'a same-chat refresh must apply')
  } finally {
    await cleanup()
  }
})

let failed = 0
for (const { name, fn } of tests) {
  try {
    await fn()
    console.log(`ok - ${name}`)
  } catch (e) {
    failed += 1
    console.log(`FAIL - ${name}`)
    console.log(`  ${e && e.message}`)
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`)
process.exit(failed === 0 ? 0 : 1)
