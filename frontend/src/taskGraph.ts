import { MarkerType, type Edge, type Node } from '@xyflow/react'
import type { RunEvent, Task } from './api'

export type TraceNodeData = {
  title: string; subtitle: string; status: string; elapsed?: string; detail?: string; runId?: string
  taskId?: string; eventNode?: string; visits?: number; tools?: string[]; kind?: 'task' | 'step' | 'system'; root?: boolean
}
export type TraceGraph = { nodes: Node<TraceNodeData>[]; edges: Edge[]; warnings: string[] }
const domainNames: Record<string, string> = { weather: '天气', navigation: '地点与路线', music: '音乐', planning: '行程提醒', wellbeing: '健康知识', local_life: '本地生活', chat: '对话' }
const phaseNames: Record<string, string> = { memory: '准备上下文', route: '意图路由', load_skill: '加载领域规则', clarify: '参数校验与澄清', execute: '查询与生成', select: '候选选择', authorize: '权限与操作确认', perform: '执行操作', respond: '结果校验与回复' }
const terminal = ['completed', 'partial', 'failed', 'cancelled', 'interrupted']
const WIDTH = 296
const GAP = 80
const STEP_HEIGHT = 114

function eventType(event: RunEvent) { return event.type.replace(/^subtask\./, '') }
function phaseTitle(name: string) { return phaseNames[name.split('.').pop() || ''] || name }
function elapsed(start?: string, end?: string) {
  const ms = new Date(end || '').getTime() - new Date(start || '').getTime()
  return Number.isFinite(ms) && ms >= 0 ? ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s` : undefined
}

export function executionStepStatus(events: RunEvent[], name: string, taskStatus?: string): string {
  const started = [...events].reverse().find(event => eventType(event) === 'node.started' && event.node === name)
  const completed = [...events].reverse().find(event => eventType(event) === 'node.completed' && event.node === name)
  const active = started && (!completed || started.seq > completed.seq)
  if (active && ['cancelled', 'interrupted', 'failed'].includes(taskStatus || '')) return taskStatus!
  if (completed && !active) {
    if (['not_needed', 'skipped', 'read_only'].includes(completed.status)) return 'skipped'
    if (completed.status === 'rejected') return 'cancelled'
    return ['failed', 'cancelled', 'interrupted'].includes(completed.status) ? completed.status : 'completed'
  }
  const waiting = events.some(event => eventType(event) === 'interrupt.required'
    && event.seq > Math.max(completed?.seq || 0, started?.seq || 0)
    && ((event.reason === 'approval' || event.kind === 'approval') && name.endsWith('.authorize')
      || (event.reason === 'selection' || event.kind === 'selection') && name.endsWith('.select')
      || (event.reason === 'clarification' || event.kind === 'clarification') && name.endsWith('.clarify')))
  return waiting ? 'waiting' : active ? 'running' : 'idle'
}

// Tool/model events often have no node field. Attribute them within each task's active step.
export function eventsForGraphNode(events: RunEvent[], data: TraceNodeData): RunEvent[] {
  const scoped = data.root ? events.filter(event => !event.child_task_id) : data.taskId ? events.filter(event => (event.child_task_id || event.task_id) === data.taskId) : events.filter(event => !event.child_task_id)
  if (!data.eventNode) return scoped
  let current = ''
  return scoped.filter(event => {
    if (eventType(event) === 'node.started') current = event.node
    const belongs = event.node === data.eventNode || (!event.node && current === data.eventNode)
    if (eventType(event) === 'node.completed' && event.node === current) current = ''
    return belongs
  })
}

function branchStatus(child: Task, events: RunEvent[]): string {
  if (terminal.includes(child.status)) return child.status
  const lifecycle = [...events].reverse().find(event => eventType(event) === 'interrupt.required' || eventType(event).startsWith('task.'))
  const kind = lifecycle && eventType(lifecycle)
  const status = kind === 'task.started' ? 'running' : kind === 'task.retry_requested' ? 'queued'
    : kind === 'interrupt.required' ? 'waiting_user' : kind?.startsWith('task.') ? kind.slice(5) : child.status
  return status === 'waiting_user' ? 'waiting' : status || 'queued'
}

function executionNodes(task: Task, events: RunEvent[], parentId?: string): Node<TraceNodeData>[] {
  const names = [...new Set(events.filter(event => ['node.started', 'node.completed'].includes(eventType(event)) && event.node).map(event => String(event.node)))]
  return names.map((name, index) => {
    const starts = events.filter(event => eventType(event) === 'node.started' && event.node === name)
    const start = starts[starts.length - 1]
    const complete = [...events].reverse().find(event => eventType(event) === 'node.completed' && event.node === name && event.seq >= (start?.seq || 0))
    const data: TraceNodeData = { title: phaseTitle(name), subtitle: name, status: executionStepStatus(events, name, task.status),
      taskId: task.id, eventNode: name, visits: starts.length, elapsed: elapsed(start?.created_at, complete?.created_at), kind: 'step', root: !parentId }
    data.tools = [...new Set(eventsForGraphNode(events, data).filter(event => eventType(event) === 'tool.started').map(event => String(event.tool_name || '')).filter(Boolean))]
    return { id: parentId ? `${task.id}::${name}` : name, type: 'trace', position: { x: parentId ? 16 : 0, y: parentId ? 112 + index * STEP_HEIGHT : index * STEP_HEIGHT },
      ...(parentId ? { parentId, extent: 'parent' as const } : {}), style: { width: WIDTH }, data }
  })
}

function connect(nodes: Node<TraceNodeData>[], edges: Edge[], source: string, target: string, label?: string, dependency = false) {
  const from = nodes.find(node => node.id === source)
  const to = nodes.find(node => node.id === target)
  if (!from || !to) return
  const color = dependency ? '#5484b8' : from.data.status === 'completed' ? '#459775' : '#a4afbd'
  edges.push({ id: `${source}->${target}`, source, target, type: 'smoothstep', label,
    animated: to.data.status === 'running' && from.data.status === 'completed',
    markerEnd: { type: MarkerType.ArrowClosed, color },
    style: { stroke: color, strokeWidth: dependency ? 2 : 1.5, ...(dependency ? { strokeDasharray: '6 4' } : {}) },
    labelStyle: { fill: dependency ? '#38628a' : '#63716b', fontSize: 11 }, labelBgPadding: [6, 4], labelBgBorderRadius: 4,
  })
}

export function makeExecutionGraph(task: Task, events: RunEvent[]): TraceGraph {
  const nodes = executionNodes(task, events)
  if (!nodes.length) nodes.push({ id: 'plan', type: 'trace', position: { x: 0, y: 0 }, style: { width: WIDTH },
    data: { title: '任务规划与接入', subtitle: task.query, status: terminal.includes(task.status) ? task.status : 'running', taskId: task.id, kind: 'system', root: true } })
  const edges: Edge[] = []
  nodes.slice(1).forEach((node, index) => connect(nodes, edges, nodes[index].id, node.id))
  return { nodes, edges, warnings: [] }
}

export function makeTaskGraph(task: Task, events: RunEvent[], expanded = false): TraceGraph {
  const children = task.children || []
  const warnings: string[] = []
  const specs = children.map(child => {
    let spec: { id?: string; depends_on?: unknown; bind_slots?: Record<string, string> } = {}
    try { spec = JSON.parse(child.plan_json || '{}') || {} } catch { warnings.push(`任务 ${child.id.slice(0, 8)} 的计划无法解析`) }
    return { child, key: spec.id || child.id, dependencies: Array.isArray(spec.depends_on) ? [...new Set(spec.depends_on.filter((key): key is string => typeof key === 'string'))] : [], bindings: spec.bind_slots || {} }
  })
  const byKey = new Map(specs.map(item => [item.key, item]))
  const ranks = new Map<string, number>()
  // Bounded topological layout: never recurse forever on corrupt historical plans.
  for (let pass = 0; pass < specs.length; pass++) {
    for (const item of specs) {
      if (!ranks.has(item.key) && item.dependencies.every(key => ranks.has(key)))
        ranks.set(item.key, item.dependencies.length ? 1 + Math.max(...item.dependencies.map(key => ranks.get(key)!)) : 0)
    }
  }
  const invalid = new Set(specs.filter(item => !ranks.has(item.key)).map(item => item.key))
  if (invalid.size) warnings.push('部分依赖缺失或成环，已隔离展示；请检查原始计划。')
  const layers = new Map<number, typeof specs>()
  specs.forEach(item => {
    const level = ranks.get(item.key) ?? Math.max(0, ...ranks.values()) + 1
    layers.set(level, [...(layers.get(level) || []), item])
  })
  const laneWidth = WIDTH + (expanded ? 32 : 0)
  const pitch = laneWidth + GAP
  const widest = Math.max(1, ...[...layers.values()].map(layer => layer.length))
  const center = (widest - 1) * pitch / 2
  const nodes: Node<TraceNodeData>[] = [{ id: 'plan', type: 'trace', position: { x: center + (expanded ? 16 : 0), y: 0 }, style: { width: WIDTH },
    data: { title: '任务规划与分发', subtitle: `${children.length} 个子任务 · ${specs.filter(item => !item.dependencies.length).length} 个无依赖入口`, status: 'completed', kind: 'system' } }]
  const edges: Edge[] = []
  let y = 150
  for (const [, layer] of [...layers].sort(([a], [b]) => a - b)) {
    let maxHeight = 0
    layer.forEach((item, index) => {
      const { child } = item
      const childEvents = events.filter(event => event.child_task_id === child.id)
      const status = branchStatus(child, childEvents)
      const stages = expanded ? executionNodes({ ...child, status: status === 'waiting' ? 'waiting_user' : status }, childEvents, child.id) : []
      const latest = [...childEvents].reverse().find(event => event.type === 'node.started')
      const dependencies = item.dependencies.map(key => byKey.get(key)?.child.query || key).join('；')
      const data: TraceNodeData = { title: `${domainNames[child.domain || ''] || child.domain || '任务'}子图`,
        subtitle: expanded ? child.query : status === 'blocked' ? `等待：${dependencies}` : phaseNames[String(latest?.node || '').split('.').pop() || ''] || child.query,
        status, taskId: child.id, detail: `${child.query}${dependencies ? `\n依赖：${dependencies}` : '\n无前置依赖'}`, runId: child.id, kind: 'task' }
      const height = expanded ? 128 + Math.max(1, stages.length) * STEP_HEIGHT : 104
      maxHeight = Math.max(maxHeight, height)
      nodes.push({ id: child.id, type: expanded ? 'lane' : 'trace', position: { x: center + (index - (layer.length - 1) / 2) * pitch, y },
        style: { width: laneWidth, ...(expanded ? { height } : {}) }, data })
      nodes.push(...stages)
      stages.slice(1).forEach((stage, i) => connect(nodes, edges, stages[i].id, stage.id))
    })
    y += maxHeight + 120
  }
  nodes.push({ id: 'aggregate', type: 'trace', position: { x: center + (expanded ? 16 : 0), y }, style: { width: WIDTH },
    data: { title: '结果汇总', subtitle: `${children.filter(child => child.status === 'completed').length}/${children.length} 项已完成`,
      status: terminal.includes(task.status) ? task.status : task.status === 'waiting_user' ? 'waiting' : 'running', kind: 'system' } })
  const dependedOn = new Set(specs.flatMap(item => item.dependencies))
  specs.forEach(({ child, key, dependencies, bindings }) => {
    if (!dependencies.length) connect(nodes, edges, 'plan', child.id, '无前置依赖')
    else dependencies.forEach(dependency => {
      if (invalid.has(key) || !byKey.has(dependency)) return
      const fields = Object.entries(bindings).filter(([, path]) => typeof path === 'string' && path.split('.')[0] === dependency).map(([field]) => field)
      connect(nodes, edges, byKey.get(dependency)!.child.id, child.id, fields.length ? `结果依赖 · ${fields.join(' / ')}` : '完成后执行', true)
    })
    if (!dependedOn.has(key) || invalid.has(key)) connect(nodes, edges, child.id, 'aggregate', '结果汇总')
  })
  return { nodes, edges, warnings }
}
