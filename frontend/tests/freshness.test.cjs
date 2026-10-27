const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/freshness.ts'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } })
const generated = { exports: {} }
new Function('module', 'exports', compiled.outputText)(generated, generated.exports)
const { interruptExpired } = generated.exports

test('candidate buttons expire at deadline and reject legacy or invalid timestamps', () => {
  const now = Date.parse('2026-10-24T08:00:00Z')
  assert.equal(interruptExpired({ expires_at: '2026-10-24T08:00:01Z' }, now), false)
  assert.equal(interruptExpired({ expires_at: '2026-10-24T08:00:00Z' }, now), true)
  assert.equal(interruptExpired({ expires_at: 'bad' }, now), true)
  assert.equal(interruptExpired({}, now), true)
  assert.equal(interruptExpired({ expires_at: '2027-10-24T08:00:00Z', expired: true }, now), true)
})
