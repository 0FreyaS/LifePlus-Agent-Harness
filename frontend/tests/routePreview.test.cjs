const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const { StaticRouter } = require('react-router-dom/server')

function compile(name, resolve) {
  const source = fs.readFileSync(path.join(__dirname, '../src', name), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } })
  const generated = { exports: {} }
  new Function('require', 'module', 'exports', compiled.outputText)(resolve, generated, generated.exports)
  return generated.exports
}

const display = compile('routeDisplay.ts', require)
const { RoutePreview } = compile('RoutePreview.tsx', name => {
  if (name === './routeDisplay') return display
  if (name === './api') return { api: {} }
  if (name === './routePlanning' || name.endsWith('.css')) return {}
  return require(name)
})

test('rendered selectors keep three transport choices and select only the requested default', () => {
  for (const mode of ['bicycling', 'walking', 'driving']) {
    const route = { task_id: 'task', job_id: 'job', travel_mode: mode, default_travel_mode: mode,
      option_id: 0, options: [], origin_name: '杭州东站', destination_name: '西湖', distance_m: 1000,
      duration_s: 500, map_zoom: 12, map_zoom_large: 13, navigation_url: 'https://uri.amap.com/navigation' }
    // Component markup only: effects, network requests, browser layout and clicks are not exercised.
    const html = renderToStaticMarkup(React.createElement(StaticRouter, { location: '/' }, React.createElement(RoutePreview, { route })))
    const start = html.indexOf('aria-label="出行方式"')
    const controls = html.slice(start, html.indexOf('class="route-refresh-plan"', start))
    assert.equal((controls.match(/aria-pressed=/g) || []).length, 3)
    assert.equal((controls.match(/aria-pressed="true"/g) || []).length, 1)
    assert.ok(controls.includes(`aria-label="${display.routeMode(mode)}，当前选择"`))
    assert.ok(controls.indexOf(`aria-label="${display.routeMode(mode)}，当前选择"`) < controls.indexOf('aria-pressed="false"'))
    for (const label of ['骑行', '驾车', '步行']) assert.ok(controls.includes(label))
  }
})

test('the LifePlus route reply includes transport choices above its map and real schemes below', () => {
  const route = { task_id: 'task', job_id: 'job', travel_mode: 'driving', default_travel_mode: 'driving',
    option_id: 0, options: [0, 1, 2].map(option_id => ({ option_id, label: `方案 ${option_id + 1}`, distance_m: 8600 + option_id * 100,
      duration_s: 1500 + option_id * 100, tolls: '0' })), origin_name: '杭州东站', destination_name: '西湖',
    distance_m: 8600, duration_s: 1500, navigation_url: 'https://uri.amap.com/navigation' }
  const html = renderToStaticMarkup(React.createElement(StaticRouter, { location: '/' }, React.createElement(RoutePreview, { route })))
  assert.ok(html.startsWith('<article'))
  assert.ok(html.includes('aria-label="出行方式"'))
  assert.ok(html.includes('aria-label="可选路线方案"'))
  assert.equal((html.match(/aria-pressed=/g) || []).length, 6)
  assert.equal((html.match(/aria-pressed="true"/g) || []).length, 2)
  assert.ok(html.includes('方案 3'))
  assert.ok(html.indexOf('aria-label="出行方式"') < html.indexOf('class="route-preview-map"'))
  assert.ok(html.indexOf('aria-label="可选路线方案"') > html.indexOf('class="route-preview-map"'))
  assert.ok(!html.includes('route-selection-panel'))
})

test('chat route controls belong only to LifePlus replies, never to the page header', () => {
  const source = ts.createSourceFile('App.tsx', fs.readFileSync(path.join(__dirname, '../src/App.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  let pickers = 0
  function visit(node, classes = []) {
    if (ts.isJsxElement(node)) {
      const className = node.openingElement.attributes.properties.find(attribute => ts.isJsxAttribute(attribute) && attribute.name.getText(source) === 'className')
      if (className?.initializer) classes = [...classes, ts.isStringLiteral(className.initializer) ? className.initializer.text : className.initializer.getText(source)]
    }
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(source) === 'RoutePreview') {
      pickers++
      assert.ok(classes.includes('conversation-scroll'))
      assert.ok(!classes.includes('chat-heading'))
      assert.ok(!node.attributes.properties.some(attribute => ts.isJsxAttribute(attribute) && attribute.name.getText(source) === 'selectionOnly'))
      assert.ok(classes.some(value => value.includes('message-content')))
    }
    ts.forEachChild(node, child => visit(child, classes))
  }
  visit(source)
  assert.equal(pickers, 2)
})
