const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/routeDisplay.ts'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } })
const generated = { exports: {} }
new Function('module', 'exports', compiled.outputText)(generated, generated.exports)
const { routeDistance, routeDuration, routeMode, routeModeOrder, safeNavigationUrl, messageTaskId, withoutStandaloneRouteLinks } = generated.exports
test('route labels use real units and do not claim unknown metrics as zero', () => {
  assert.equal(routeDistance(800), '800 米')
  assert.equal(routeDistance(1250), '1.3 公里')
  assert.equal(routeDistance(NaN), '距离未知')
  assert.equal(routeDuration(61), '2 分钟')
  assert.equal(routeDuration(7200), '2 小时')
  assert.equal(routeDuration(0), '用时未知')
  assert.equal(routeMode('walking'), '步行')
})
test('external navigation accepts only the official HTTPS navigation endpoint', () => {
  assert.ok(safeNavigationUrl('https://uri.amap.com/navigation?from=120,30&to=121,31'))
  for (const value of ['javascript:alert(1)', 'https://uri.amap.com.evil.example/navigation', 'http://uri.amap.com/navigation', 'https://evil.example', '/navigation']) assert.equal(safeNavigationUrl(value), null)
})
test('history binds map attachments to the matching task rather than the latest task', () => {
  assert.equal(messageTaskId('task-a:assistant'), 'task-a')
  assert.equal(messageTaskId('task-b:user'), 'task-b')
  assert.equal(messageTaskId(undefined), '')
})
test('an attached map replaces standalone navigation URLs without hiding unrelated evidence', () => {
  assert.equal(withoutStandaloneRouteLinks('步行约 2 公里。\nhttps://uri.amap.com/navigation?from=120,30&to=121,31'), '步行约 2 公里。')
  assert.equal(withoutStandaloneRouteLinks('资料：https://example.com/reference'), '资料：https://example.com/reference')
  assert.equal(withoutStandaloneRouteLinks('https://uri.amap.com/navigation?from=120,30&to=121,31'), '路线已规划。')
})

test('the requested mode is first without hiding any alternative transport choice', () => {
  for (const mode of ['bicycling', 'walking', 'driving']) {
    const choices = routeModeOrder(mode)
    assert.equal(choices[0], mode)
    assert.deepEqual([...choices].sort(), ['bicycling', 'driving', 'walking'])
    assert.equal(choices.length, 3)
  }
  assert.equal(routeModeOrder('unknown')[0], 'driving')
})
