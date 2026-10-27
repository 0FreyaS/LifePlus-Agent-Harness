const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/routePlanning.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
const generated = { exports: {} }
new Function('require', 'module', 'exports', compiled.outputText)(require, generated, generated.exports)
const { waitForPlannedRoute } = generated.exports

test('mode switch waits for a completed real task before fetching its receipt', async () => {
  const statuses = ['queued', 'running', 'completed']
  let calls = 0
  const receipt = { task_id: 'new-task', travel_mode: 'driving', option_id: 0 }
  const result = await waitForPlannedRoute({ task: async id => { assert.equal(id, 'new-task'); return { status: statuses.shift() } },
    route: async id => { calls++; assert.equal(id, 'new-task'); return receipt } }, 'new-task', new AbortController().signal, 1000, 1)
  assert.equal(result, receipt)
  assert.equal(calls, 1)
})

test('failure, cancellation and unexpected clarification never return an old receipt as a new route', async () => {
  for (const status of ['failed', 'partial', 'cancelled', 'interrupted', 'waiting_user']) {
    let calls = 0
    await assert.rejects(waitForPlannedRoute({ task: async () => ({ status, answer: '路线服务失败' }),
      route: async () => { calls++; return {} } }, 'new-task', new AbortController().signal), /失败|补充信息/)
    assert.equal(calls, 0)
  }
})

test('an aborted view cannot receive a late completed route', async () => {
  const controller = new AbortController()
  let release
  let calls = 0
  const pending = waitForPlannedRoute({ task: () => new Promise(resolve => { release = resolve }), route: async () => { calls++; return {} } }, 'task', controller.signal)
  controller.abort()
  release({ status: 'completed' })
  await assert.rejects(pending, { name: 'AbortError' })
  assert.equal(calls, 0)
})

test('polling has an explicit deadline rather than an unbounded loop', async () => {
  let calls = 0
  await assert.rejects(waitForPlannedRoute({ task: async () => { calls++; return { status: 'running' } }, route: async () => ({}) },
    'task', new AbortController().signal, 0), /超时/)
  assert.equal(calls, 0)
})

test('leaving the view cancels even a long polling pause promptly', async () => {
  const controller = new AbortController()
  const pending = waitForPlannedRoute({ task: async () => ({ status: 'running' }), route: async () => ({}) }, 'task', controller.signal, 90000, 10000)
  setTimeout(() => controller.abort(), 5)
  await assert.rejects(pending, { name: 'AbortError' })
})

test('a stalled network request cannot extend the polling deadline indefinitely', async () => {
  await assert.rejects(waitForPlannedRoute({ task: () => new Promise(() => {}), route: async () => ({}) },
    'task', new AbortController().signal, 5), /超时/)
})
