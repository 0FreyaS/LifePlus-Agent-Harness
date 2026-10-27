const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')

const source = fs.readFileSync(path.join(__dirname, '../src/taskGraph.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
const generated = { exports: {} }
new Function('require', 'module', 'exports', compiled.outputText)(require, generated, generated.exports)
const { executionStepStatus, makeTaskGraph, makeExecutionGraph, eventsForGraphNode } = generated.exports

function child(id, depends_on = [], status = 'queued') {
  return { id, query: `query-${id}`, domain: id === 'weather' ? 'weather' : 'music', status,
    plan_json: JSON.stringify({ id, depends_on }) }
}
function edgePairs(graph) { return graph.edges.map(edge => `${edge.source}->${edge.target}`).sort() }

test('retry queues only the failed branch while preserving completed siblings', () => {
  const graph = makeTaskGraph({ status: 'running', children: [child('weather', [], 'completed'), child('music', [], 'queued')] }, [
    { child_task_id: 'music', type: 'subtask.task.failed' },
    { child_task_id: 'music', type: 'subtask.task.retry_requested' },
  ])
  assert.equal(graph.nodes.find(node => node.id === 'weather').data.status, 'completed')
  assert.equal(graph.nodes.find(node => node.id === 'music').data.status, 'queued')
})

test('independent domains fork into the same layer then join; nodes do not overlap', () => {
  const graph = makeTaskGraph({ status: 'running', children: [child('weather'), child('music')] }, [])
  assert.deepEqual(edgePairs(graph), ['music->aggregate', 'plan->music', 'plan->weather', 'weather->aggregate'])
  const nodes = graph.nodes.filter(node => ['weather', 'music'].includes(node.id))
  assert.equal(nodes[0].position.y, nodes[1].position.y)
  assert.ok(Math.abs(nodes[0].position.x - nodes[1].position.x) > 360)
})

test('only actual dependencies create sequential edges and deeper layers', () => {
  const graph = makeTaskGraph({ status: 'running', children: [child('weather'), child('save', ['weather']), child('music')] }, [])
  const node = id => graph.nodes.find(item => item.id === id)
  assert.ok(node('save').position.y > node('weather').position.y)
  assert.equal(node('weather').position.y, node('music').position.y)
  assert.deepEqual(edgePairs(graph), ['music->aggregate', 'plan->music', 'plan->weather', 'save->aggregate', 'weather->save'])
})

test('SSE lifecycle and node events show active branch without waiting for a group refresh', () => {
  const graph = makeTaskGraph({ status: 'running', children: [child('weather'), child('music')] }, [
    { child_task_id: 'weather', type: 'subtask.task.started' },
    { child_task_id: 'weather', type: 'node.started', node: 'weather.execute' },
    { child_task_id: 'music', type: 'subtask.interrupt.required' },
  ])
  const weather = graph.nodes.find(node => node.id === 'weather')
  assert.equal(weather.data.status, 'running')
  assert.equal(weather.data.subtitle, '查询与生成')
  assert.equal(graph.nodes.find(node => node.id === 'music').data.status, 'waiting')
  assert.ok(graph.edges.find(edge => edge.target === 'weather').animated)
})

test('terminal snapshot overrides stale running or interrupt events after cancellation', () => {
  const graph = makeTaskGraph({ status: 'cancelled', children: [child('music', [], 'cancelled')] }, [
    { child_task_id: 'music', type: 'subtask.task.started' },
    { child_task_id: 'music', type: 'subtask.interrupt.required' },
  ])
  assert.equal(graph.nodes.find(node => node.id === 'music').data.status, 'cancelled')
  assert.equal(graph.nodes.find(node => node.id === 'aggregate').data.status, 'cancelled')
})

test('fan-in waits below every predecessor and independent branches have distinct positions', () => {
  const graph = makeTaskGraph({ status: 'running', children: [
    ...Array.from({ length: 5 }, (_, i) => child(`t${i}`)), child('join', ['t0', 't4']),
  ] }, [])
  const ready = graph.nodes.filter(node => /^t\d$/.test(node.id))
  assert.equal(new Set(ready.map(node => node.position.x)).size, 5)
  assert.equal(new Set(ready.map(node => node.position.y)).size, 1)
  assert.ok(graph.nodes.find(node => node.id === 'join').position.y > ready[0].position.y)
  assert.ok(graph.edges.some(edge => edge.source === 't0' && edge.target === 'join'))
  assert.ok(graph.edges.some(edge => edge.source === 't4' && edge.target === 'join'))
})

test('single-domain cancellation marks only the unfinished step, not completed predecessors', () => {
  const events = [{ type: 'node.started', node: 'memory', seq: 1 }, { type: 'node.completed', node: 'memory', seq: 2 },
    { type: 'node.started', node: 'music.select', seq: 3 }, { type: 'interrupt.required', kind: 'selection', seq: 4 }]
  assert.equal(executionStepStatus(events, 'memory', 'failed'), 'completed')
  assert.equal(executionStepStatus(events, 'music.select', 'waiting_user'), 'waiting')
  assert.equal(executionStepStatus(events, 'music.select', 'cancelled'), 'cancelled')
  assert.equal(executionStepStatus(events, 'respond', 'cancelled'), 'idle')
})

test('a resumed step is running after a newer start even if an earlier completion exists', () => {
  const events = [{ type: 'node.started', node: 'music.select', seq: 1 },
    { type: 'node.completed', node: 'music.select', seq: 2 }, { type: 'node.started', node: 'music.select', seq: 3 }]
  assert.equal(executionStepStatus(events, 'music.select', 'running'), 'running')
  assert.equal(executionStepStatus([{ type: 'node.started', node: 'music.select', seq: 1 },
    { type: 'interrupt.required', kind: 'selection', seq: 2 },
    { type: 'node.started', node: 'music.select', seq: 3 }], 'music.select', 'running'), 'running')
})

function event(seq, type, node, child_task_id, extra = {}) {
  return { seq, event_id: `event-${seq}`, type, node, child_task_id, created_at: new Date(1700000000000 + seq * 100).toISOString(), ...extra }
}

test('expanded graph preserves parallel lanes and scopes every phase to its own task', () => {
  const events = [event(1, 'node.started', 'memory', 'weather'), event(2, 'node.started', 'memory', 'music'),
    event(3, 'node.completed', 'memory', 'weather'), event(4, 'node.started', 'weather.execute', 'weather')]
  const graph = makeTaskGraph({ status: 'running', children: [child('weather', [], 'running'), child('music', [], 'running')] }, events, true)
  const lane = id => graph.nodes.find(node => node.id === id)
  assert.equal(lane('weather').type, 'lane')
  assert.equal(lane('weather').position.y, lane('music').position.y)
  assert.equal(lane('weather::memory').parentId, 'weather')
  assert.equal(lane('music::memory').parentId, 'music')
  assert.equal(lane('weather::memory').data.status, 'completed')
  assert.equal(lane('music::memory').data.status, 'running')
  assert.ok(!graph.edges.some(edge => edge.source.startsWith('weather::') && edge.target.startsWith('music::')))
})

test('expanded fan-in starts below the tallest predecessor, with binding labels', () => {
  const save = child('save', ['weather', 'music'], 'blocked')
  save.plan_json = JSON.stringify({ id: 'save', depends_on: ['weather', 'music'], bind_slots: { details: 'weather.result' } })
  const graph = makeTaskGraph({ status: 'running', children: [child('weather'), child('music'), save] },
    ['memory', 'route', 'weather.execute', 'respond'].map((name, i) => event(i, 'node.started', name, 'weather')), true)
  const get = id => graph.nodes.find(node => node.id === id)
  assert.ok(get('save').position.y > get('weather').position.y + get('weather').style.height)
  assert.ok(get('save').position.y > get('music').position.y + get('music').style.height)
  assert.match(graph.edges.find(edge => edge.source === 'weather' && edge.target === 'save').label, /details/)
  assert.equal(get('save').data.status, 'blocked')
})

test('all expanded nodes fit their lanes, and branches and layers do not overlap', () => {
  const children = [...Array.from({ length: 5 }, (_, i) => child(`t${i}`)), child('join', ['t0', 't4'])]
  const events = children.flatMap((item, i) => ['memory', 'route', 'music.execute', 'music.select', 'respond'].map((name, j) => event(i * 10 + j, 'node.started', name, item.id)))
  const graph = makeTaskGraph({ status: 'running', children }, events, true)
  for (const node of graph.nodes.filter(node => node.parentId)) {
    const lane = graph.nodes.find(item => item.id === node.parentId)
    assert.ok(node.position.x >= 0 && node.position.x + node.style.width <= lane.style.width)
    assert.ok(node.position.y >= 100 && node.position.y + 100 <= lane.style.height)
  }
  const lanes = graph.nodes.filter(node => node.type === 'lane')
  for (let i = 0; i < lanes.length; i++) for (let j = i + 1; j < lanes.length; j++) {
    const a = lanes[i], b = lanes[j]
    assert.ok(a.position.x + a.style.width < b.position.x || b.position.x + b.style.width < a.position.x
      || a.position.y + a.style.height < b.position.y || b.position.y + b.style.height < a.position.y)
  }
})

test('single task displays only observed steps, never a fabricated confirmation or operation', () => {
  const graph = makeExecutionGraph({ id: 'root', status: 'completed' }, [
    event(1, 'node.started', 'memory'), event(2, 'node.completed', 'memory'),
    event(3, 'node.started', 'weather.execute'), event(4, 'node.completed', 'weather.execute'),
    event(5, 'node.started', 'respond'), event(6, 'node.completed', 'respond'),
  ])
  assert.deepEqual(graph.nodes.map(node => node.id), ['memory', 'weather.execute', 'respond'])
  assert.deepEqual(edgePairs(graph), ['memory->weather.execute', 'weather.execute->respond'])
})

test('single-task REST trace without task_id still attributes actual tools to the step', () => {
  const events = [event(1, 'node.started', 'navigation.execute'), event(2, 'tool.started', undefined, undefined, { tool_name: 'maps_search' }),
    event(3, 'tool.completed'), event(4, 'node.completed', 'navigation.execute'), event(5, 'answer.delta')]
  const graph = makeExecutionGraph({ id: 'root', status: 'running' }, events)
  assert.deepEqual(graph.nodes[0].data.tools, ['maps_search'])
  assert.deepEqual(eventsForGraphNode(events, graph.nodes[0].data).map(item => item.seq), [1, 2, 3, 4])
})

test('interleaved tools, interrupts and model events never leak between parallel tasks', () => {
  const events = [event(1, 'node.started', 'weather.execute', 'weather'), event(2, 'node.started', 'music.execute', 'music'),
    event(3, 'tool.started', undefined, 'weather', { tool_name: 'maps_weather' }),
    event(4, 'tool.started', undefined, 'music', { tool_name: 'search_music' }),
    event(5, 'model.completed', undefined, 'weather'), event(6, 'node.completed', 'weather.execute', 'weather'),
    event(7, 'subtask.interrupt.required', undefined, 'music', { kind: 'selection' })]
  const graph = makeTaskGraph({ status: 'running', children: [child('weather'), child('music')] }, events, true)
  const weather = graph.nodes.find(node => node.id === 'weather::weather.execute')
  assert.deepEqual(weather.data.tools, ['maps_weather'])
  assert.deepEqual(eventsForGraphNode(events, weather.data).map(item => item.seq), [1, 3, 5, 6])
})

test('resume reuses the phase node, records visits and uses the latest attempt duration', () => {
  const events = [event(1, 'node.started', 'music.select'), event(2, 'interrupt.required', undefined, undefined, { kind: 'selection' }),
    event(3, 'node.started', 'music.select'), event(4, 'node.completed', 'music.select')]
  const graph = makeExecutionGraph({ id: 'root', status: 'running' }, events)
  assert.equal(graph.nodes.length, 1)
  assert.equal(graph.nodes[0].data.visits, 2)
  assert.equal(graph.nodes[0].data.status, 'completed')
  assert.equal(graph.nodes[0].data.elapsed, '100 ms')
})

test('planning failures still have an inspectable status node', () => {
  const graph = makeExecutionGraph({ id: 'root', status: 'failed', query: 'q' }, [])
  assert.equal(graph.nodes.length, 1)
  assert.equal(graph.nodes[0].data.status, 'failed')
})

test('missing dependencies or cycles cannot recurse forever or fabricate a ready entry', () => {
  for (const children of [[child('a', ['missing'])], [child('a', ['b']), child('b', ['a'])]]) {
    const graph = makeTaskGraph({ status: 'failed', children }, [], true)
    assert.ok(graph.warnings.length)
    assert.ok(!graph.edges.some(edge => edge.source === 'plan'))
    assert.ok(graph.nodes.every(node => Number.isFinite(node.position.y)))
  }
})

test('malformed historical plans cannot crash the execution page', () => {
  const broken = child('broken')
  broken.plan_json = '{'
  const graph = makeTaskGraph({ status: 'running', children: [broken] }, [])
  assert.equal(graph.warnings.length, 1)
  assert.ok(graph.nodes.some(node => node.id === 'broken'))
})

test('read-only and unnecessary action gates are skipped, not claimed as completed side effects', () => {
  for (const status of ['read_only', 'not_needed', 'skipped']) {
    assert.equal(executionStepStatus([event(1, 'node.started', 'weather.perform'),
      event(2, 'node.completed', 'weather.perform', undefined, { status })], 'weather.perform', 'completed'), 'skipped')
  }
  assert.equal(executionStepStatus([event(1, 'node.started', 'planning.authorize'),
    event(2, 'node.completed', 'planning.authorize', undefined, { status: 'rejected' })], 'planning.authorize', 'cancelled'), 'cancelled')
})

if (process.env.TRACE_GRAPH_REPORT) test('live captured tasks map to the same parallel and dependent graph without fabricated phases', () => {
  const report = JSON.parse(fs.readFileSync(process.env.TRACE_GRAPH_REPORT, 'utf8'))
  assert.equal(report.passed, true)
  assert.ok(report.snapshots.some(snapshot => snapshot.states.filter(([, status]) => status === 'running').length >= 2))
  const graph = makeTaskGraph(report.task, report.events, true)
  assert.equal(graph.warnings.length, 0)
  assert.equal(graph.nodes.filter(node => node.type === 'lane').length, report.task.children.length)
  assert.equal(graph.edges.filter(edge => edge.source === 'plan').length, report.independent_entries)
  assert.equal(graph.edges.filter(edge => edge.style.strokeDasharray).length, report.dependencies.length)
  for (const child of report.task.children) {
    const observed = [...new Set(report.events.filter(event => event.child_task_id === child.id
      && ['node.started', 'node.completed'].includes(event.type)).map(event => event.node))]
    const stages = graph.nodes.filter(node => node.parentId === child.id)
    assert.deepEqual(stages.map(node => node.data.eventNode), observed)
    assert.equal(graph.nodes.find(node => node.id === child.id).data.status, 'completed')
    for (const stage of stages) assert.ok(eventsForGraphNode(report.events, stage.data).every(event => event.child_task_id === child.id))
  }
})

if (process.env.SHOWCASE_GRAPH_REPORT) test('the real four-task showcase expands into three parallel lanes and one dependent lane', () => {
  const report = JSON.parse(fs.readFileSync(process.env.SHOWCASE_GRAPH_REPORT, 'utf8'))
  assert.equal(report.outcome, 'completed')
  assert.equal(report.task.children.length, 4)
  const graph = makeTaskGraph(report.task, report.events, true)
  assert.equal(graph.warnings.length, 0)
  assert.equal(graph.nodes.filter(node => node.type === 'lane').length, 4)
  assert.equal(graph.edges.filter(edge => edge.source === 'plan').length, 3)
  assert.equal(graph.edges.filter(edge => edge.style.strokeDasharray).length, 1)
  for (const task of report.task.children) {
    const nodes = graph.nodes.filter(node => node.parentId === task.id)
    const observed = [...new Set(report.events.filter(event => event.child_task_id === task.id
      && ['node.started', 'node.completed'].includes(event.type)).map(event => event.node))]
    assert.deepEqual(nodes.map(node => node.data.eventNode), observed)
    for (const node of nodes) assert.ok(eventsForGraphNode(report.events, node.data).every(event => event.child_task_id === task.id))
  }
})
