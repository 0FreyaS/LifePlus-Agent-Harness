const assert = require('node:assert/strict')
const test = require('node:test')

const base = process.env.LIFEPLUS_FRONTEND_TEST_URL

test('the running Vite server serves route selectors, not an obsolete cached component', { skip: !base, timeout: 15000 }, async () => {
  const origin = new URL(base)
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname), 'Only test a local development server')
  for (const [file, markers] of [
    ['/src/RoutePreview.tsx', ['route-planning-controls', 'route-mode-tabs', 'route-options', 'replan']],
    ['/src/App.tsx', ['routeSelections', 'route-selection-load-error']],
    ['/src/RoutePreview.css', ['route-mode-tabs', 'route-options']],
  ]) {
    const response = await fetch(new URL(file, origin), { signal: AbortSignal.timeout(4000) })
    assert.equal(response.status, 200, file)
    const served = await response.text()
    for (const marker of markers) assert.ok(served.includes(marker), `${file}: running server is missing ${marker}`)
    assert.ok(!served.includes('selectionOnly') && !served.includes('route-selection-panel'), `${file}: obsolete page-level picker is still served`)
  }
})
