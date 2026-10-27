const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/taskEvents.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } })
const generated = { exports: {} }
new Function('module', 'exports', compiled.outputText)(generated, generated.exports)
const { taskEventStatus } = generated.exports

test('retry and start events retain valid active task states for SSE subscription', () => {
  assert.equal(taskEventStatus('task.retry_requested'), 'queued')
  assert.equal(taskEventStatus('task.refresh_requested'), 'queued')
  assert.equal(taskEventStatus('task.started'), 'running')
  assert.equal(taskEventStatus('task.completed'), 'completed')
  assert.equal(taskEventStatus('task.failed'), 'failed')
  assert.equal(taskEventStatus('task.unknown'), undefined)
})
