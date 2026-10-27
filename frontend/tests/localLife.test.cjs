const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/localLife.ts'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } })
const generated = { exports: {} }
new Function('module', 'exports', compiled.outputText)(generated, generated.exports)
const { venueParameters, distanceText } = generated.exports
test('route and detail queries bind the selected POI ID rather than a free-text name', () => {
  const venue = { id: 'B-actual-id', name: '同名店' }
  assert.deepEqual(venueParameters('local_detail', venue, '120.2,31.4'), { poi_id: 'B-actual-id' })
  assert.deepEqual(venueParameters('local_route', venue, '120.2,31.4', 'walking'), { poi_id: 'B-actual-id', anchor: '120.2,31.4', travel_mode: 'walking' })
})
test('distance labels distinguish unknown from actual zero', () => {
  assert.equal(distanceText(null), '距离未知')
  assert.equal(distanceText(0), '0 米')
  assert.equal(distanceText(1500), '1.5 公里')
})
