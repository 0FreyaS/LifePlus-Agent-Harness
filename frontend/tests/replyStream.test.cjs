const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/replyStream.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } })
const generated = { exports: {} }
new Function('module', 'exports', compiled.outputText)(generated, generated.exports)
const { acceptStreamSequence, applyReplyEvent } = generated.exports

test('new task has an independent SSE cursor even when the previous task has many events', () => {
  const cursors = {}
  assert.equal(acceptStreamSequence(cursors, 'old', 200), true)
  assert.equal(acceptStreamSequence(cursors, 'new', 1), true)
  assert.equal(acceptStreamSequence(cursors, 'old', 201), true)
  assert.equal(acceptStreamSequence(cursors, 'new', 2), true)
})

test('reconnected or malformed events do not duplicate model deltas', () => {
  const cursors = {}
  assert.equal(acceptStreamSequence(cursors, 'task', 1), true)
  for (const sequence of [1, 0, -1, NaN, 1.5]) assert.equal(acceptStreamSequence(cursors, 'task', sequence), false)
})

test('model text is visible after the first delta without waiting for completion', () => {
  let state = applyReplyEvent({}, { task_id: 'task', type: 'answer.started' })
  state = applyReplyEvent(state, { task_id: 'task', type: 'answer.delta', delta: '你好' })
  assert.equal(state.task, '你好')
  state = applyReplyEvent(state, { task_id: 'task', type: 'answer.delta', delta: '，欢迎' })
  assert.equal(state.task, '你好，欢迎')
})

test('parallel branches and independent root tasks keep their own reply text', () => {
  let state = {}
  for (const [child, delta] of [['weather', '无锡'], ['music', '周杰伦'], ['weather', '天气']]) {
    state = applyReplyEvent(state, { task_id: 'root', child_task_id: child, type: 'answer.delta', delta })
  }
  state = applyReplyEvent(state, { task_id: 'other-root', type: 'answer.delta', delta: '你好' })
  assert.deepEqual(state, { weather: '无锡天气', music: '周杰伦', 'other-root': '你好' })
})

test('authoritative completion or safe fallback replaces partial text rather than appending it', () => {
  let state = { child: '尚未完成的内容' }
  state = applyReplyEvent(state, { task_id: 'root', stream_id: 'child', type: 'answer.replaced', answer: '已验证的回执' })
  state = applyReplyEvent(state, { task_id: 'root', child_task_id: 'child', type: 'subtask.answer.completed', answer: '已验证的回执\nhttps://example.com' })
  assert.equal(state.child, '已验证的回执\nhttps://example.com')
})

test('tool arguments and reasoning are not displayed as user-facing text deltas', () => {
  const state = { task: '你好' }
  assert.equal(applyReplyEvent(state, { task_id: 'task', type: 'tool.started', delta: 'secret arguments' }), state)
  assert.equal(applyReplyEvent(state, { task_id: 'task', type: 'answer.delta', delta: {} }), state)
})
