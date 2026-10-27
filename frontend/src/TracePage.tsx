import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ReactFlow, Background, Controls, Handle, MiniMap, Position } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Activity, ArrowDownToLine, ArrowLeft, Check, CircleAlert, CircleCheck, CircleDashed, Clock3, CornerDownRight, GitBranch, Layers, List, LoaderCircle, Play, RefreshCw, Save, ShieldCheck, TerminalSquare } from 'lucide-react'
import { api, type Review, type RunEvent, type Task } from './api'
import type { ReactNode } from 'react'
import { eventsForGraphNode, makeExecutionGraph, makeTaskGraph, type TraceNodeData } from './taskGraph'
import './taskGraph.css'
import { taskEventStatus } from './taskEvents'

function FlowNode({ data }: { data: TraceNodeData }) {
  const Icon = data.status === 'completed' ? CircleCheck : data.status === 'running' ? LoaderCircle : data.status === 'failed' ? CircleAlert : data.status === 'waiting' ? ShieldCheck : CircleDashed
  return <div className={`flow-node ${data.status}`} title={data.detail || data.subtitle}><Handle type="target" position={Position.Top} /><div className="flow-node-icon"><Icon size={16} /></div><div className="flow-node-copy"><strong>{data.title}</strong><span>{data.subtitle}</span>{data.tools?.length ? <span className="flow-tools">{data.tools.join(' · ')}</span> : null}{(data.visits || 0) > 1 && <small className="flow-visits">进入 {data.visits} 次</small>}{data.runId && <Link to={`/runs/${data.runId}`} onClick={event => event.stopPropagation()}>查看子图链路</Link>}</div><div className="flow-node-state">{data.elapsed && <small>{data.elapsed}</small>}<span>{flowStatus(data.status)}</span></div><Handle type="source" position={Position.Bottom} /></div>
}
function LaneNode({ data }: { data: TraceNodeData }) {
  return <div className={`flow-lane ${data.status}`}><Handle type="target" position={Position.Top} /><div className="flow-lane-heading" title={data.detail}><div><strong>{data.title}</strong><span>{data.subtitle}</span></div><span className={`status-badge ${data.status}`}>{flowStatus(data.status)}</span></div><Link className="flow-lane-link nodrag" to={`/runs/${data.runId}`} onClick={event => event.stopPropagation()}>查看独立链路</Link><Handle type="source" position={Position.Bottom} /></div>
}
const nodeTypes = { trace: FlowNode, lane: LaneNode }

export default function TracePage() {
  const { taskId = '' } = useParams()
  const navigate = useNavigate()
  const [task, setTask] = useState<Task | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [diagnostics, setDiagnostics] = useState<Awaited<ReturnType<typeof api.diagnostics>> | null>(null)
  const [checkpoints, setCheckpoints] = useState<any[]>([])
  const [selected, setSelected] = useState('route')
  const [graphView, setGraphView] = useState<'dependencies' | 'expanded'>('dependencies')
  const [showTimeline, setShowTimeline] = useState(false)
  const [filter, setFilter] = useState('all')
  const [error, setError] = useState('')
  const [reconnecting, setReconnecting] = useState(false)
  const [retrying, setRetrying] = useState(false)
  const [review, setReview] = useState<Omit<Review, 'updated_at'>>({ is_bad_case: false, category: '', root_cause: '', expected_answer: '', note: '' })
  const [reviewed, setReviewed] = useState(false)
  const [savingReview, setSavingReview] = useState(false)
  const [reviewMessage, setReviewMessage] = useState('')
  const lastSeq = useRef(0)
  const currentTaskId = useRef(taskId)
  currentTaskId.current = taskId

  const refresh = useCallback(async () => {
    const [freshTask, trace, timing] = await Promise.all([api.task(taskId), api.trace(taskId), api.diagnostics(taskId).catch(() => null)])
    if (currentTaskId.current !== taskId) return
    setDiagnostics(timing)
    setTask(freshTask)
    setEvents(trace)
    lastSeq.current = trace.reduce((max, event) => Math.max(max, event.seq), 0)
    setError('')
    const history = await api.checkpoints(freshTask.session_id)
    if (currentTaskId.current !== taskId) return
    setCheckpoints(history)
    setSelected(current => current || (freshTask.children?.length ? freshTask.children[0].id : trace.find(event => event.type === 'node.started')?.node || 'plan'))
  }, [taskId])

  useEffect(() => {
    setTask(null); setEvents([]); setSelected(''); setDiagnostics(null); setCheckpoints([]); lastSeq.current = 0
    void refresh().catch(e => setError(e.message))
  }, [refresh])

  useEffect(() => {
    setReviewed(false)
    void api.review(taskId).then(value => {
      if (currentTaskId.current !== taskId) return
      if (value) {
        setReview({ is_bad_case: value.is_bad_case, category: value.category, root_cause: value.root_cause, expected_answer: value.expected_answer, note: value.note })
        setReviewed(true)
      } else setReview({ is_bad_case: false, category: '', root_cause: '', expected_answer: '', note: '' })
    }).catch(e => setReviewMessage(e.message))
  }, [taskId])

  useEffect(() => {
    if (!task || ['completed', 'partial', 'failed', 'cancelled', 'interrupted'].includes(task.status)) return
    setReconnecting(false)
    const stop = api.subscribe(task.id, lastSeq.current, event => {
      if (currentTaskId.current !== task.id) return
      lastSeq.current = Math.max(lastSeq.current, event.seq)
      setEvents(current => current.some(item => item.event_id === event.event_id) ? current : [...current, event])
      if (event.type.startsWith('task.')) setTask(current => current ? { ...current, status: taskEventStatus(event.type) || current.status, answer: event.answer || current.answer } : current)
      if (event.type === 'group.updated') setTask(current => current ? { ...current, status: event.status, children: event.children } : current)
      if (event.type === 'interrupt.required') void api.task(task.id).then(value => { if (currentTaskId.current === task.id) setTask(value) }).catch(() => undefined)
      if (event.type === 'runtime.finished' || ['task.completed', 'task.failed', 'task.partial', 'group.updated'].includes(event.type)) void api.diagnostics(task.id).then(value => { if (currentTaskId.current === task.id) setDiagnostics(value) }).catch(() => undefined)
    }, () => setReconnecting(true))
    return stop
  }, [task?.id, task?.status])

  const domain = task?.children?.length ? 'multi' : events.find(event => event.type === 'route.completed')?.domain || guessDomain(task?.query || '')
  const isGroup = Boolean(task?.children?.length)
  const graph = useMemo(() => !task ? { nodes: [], edges: [], warnings: [] } : isGroup
    ? makeTaskGraph(task, events, graphView === 'expanded') : makeExecutionGraph(task, events), [task, events, isGroup, graphView])
  const { nodes, edges } = graph
  const selectedNode = nodes.find(node => node.id === selected)
  const selectedEvents = selectedNode ? selected === 'plan' ? events.filter(event => !event.child_task_id && event.type.startsWith('plan.'))
    : selected === 'aggregate' ? events.filter(event => !event.child_task_id && (event.type === 'group.updated' || event.type.startsWith('answer.') || event.type.startsWith('task.')))
    : eventsForGraphNode(events, selectedNode.data) : []
  const visibleEvents = filter === 'selected' ? selectedEvents : filter === 'all' ? events : events.filter(event => event.type.replace(/^subtask\./, '').startsWith(filter))
  const branches = nodes.filter(node => node.data.kind === 'task')
  const selectedDetails = selectedNode?.data.detail

  function selectEvent(event: RunEvent) {
    const step = nodes.find(node => node.data.eventNode && eventsForGraphNode(events, node.data).some(item => item.event_id === event.event_id))
    setSelected(step?.id || event.child_task_id || (event.type.startsWith('plan.') ? 'plan' : event.type === 'group.updated' ? 'aggregate' : event.node || 'aggregate'))
  }

  function downloadTrace() {
    const blob = new Blob([JSON.stringify({ task, events, diagnostics }, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob); const anchor = document.createElement('a')
    anchor.href = url; anchor.download = `lifeplus-trace-${taskId.slice(0, 8)}.json`; anchor.click(); URL.revokeObjectURL(url)
  }

  async function replay(checkpointId: string) {
    try {
      const branch = await api.replay(task?.session_id || '', checkpointId)
      navigate(`/runs/${branch.task_id}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : '检查点回放失败')
    }
  }

  async function saveReview() {
    setSavingReview(true); setReviewMessage('')
    try { await api.saveReview(taskId, review); setReviewed(true); setReviewMessage('归因已保存') }
    catch (e) { setReviewMessage(e instanceof Error ? e.message : '保存失败') }
    finally { setSavingReview(false) }
  }

  async function retryFailed() {
    setRetrying(true); setError('')
    try { await api.retry(taskId); await refresh() }
    catch (e) { setError(e instanceof Error ? e.message : '任务重试失败') }
    finally { setRetrying(false) }
  }

  return <div className="trace-page">
    <div className="trace-header"><div><Link className="back-link" to={task ? `/chat/${task.session_id}` : '/'}><ArrowLeft size={15} />返回对话</Link><div className="trace-title-row"><h1>Agent 执行链路</h1>{task && <span className={`status-badge ${task.status}`}><i />{flowStatus(task.status === 'waiting_user' ? 'waiting' : task.status)}</span>}</div><p>{task?.query || '正在读取任务…'}</p></div><div className="trace-actions">{task?.can_retry && <button className="secondary-button" disabled={retrying} onClick={() => void retryFailed()}><RefreshCw size={15} />{retrying ? '重试中…' : '重试失败任务'}</button>}<button className="secondary-button" onClick={() => void refresh()}><RefreshCw size={15} />刷新</button><button className="secondary-button" onClick={downloadTrace} disabled={!events.length}><ArrowDownToLine size={15} />导出轨迹</button></div></div>
    {error && <div className="inline-error trace-error"><CircleAlert size={15} />{error}<button className="secondary-button small" onClick={() => void refresh()}>重试</button></div>}
    <section className="exchange-record"><div className="exchange-heading"><div><span className="section-kicker">USER ↔ AGENT</span><h2>用户与 Agent 本轮数据</h2></div><Link className="back-link" to="/runs"><ArrowLeft size={14} />返回执行数据</Link></div><div className="exchange-columns"><div><small>用户原始输入</small><p>{task?.query || '读取中…'}</p></div><div><small>Agent 最终回复</small><p>{task?.answer || (task?.status === 'failed' ? '任务失败，请通过事件轨迹查看错误详情。' : '当前还没有最终回复')}</p></div></div></section>
    <div className="trace-metrics"><Metric icon={<Activity size={15} />} label="运行状态" value={statusLabel(task?.status)} /><Metric icon={<GitBranch size={15} />} label="当前领域" value={domainLabel(domain)} /><Metric icon={<Clock3 size={15} />} label="事件数量" value={`${events.length} 条`} /><Metric icon={<ShieldCheck size={15} />} label="执行模式" value={task?.mode === 'demo' ? '离线演示' : task?.mode === 'live' ? '真实接入' : '—'} /></div>
    {diagnostics && <div className="trace-metrics"><Metric icon={<Clock3 size={15} />} label="会话墙钟耗时" value={`${(diagnostics.wall_ms / 1000).toFixed(1)} s`} /><Metric icon={<Activity size={15} />} label="模型尝试累计" value={diagnostics.work.model_attempts ? `${(diagnostics.work.model_ms / 1000).toFixed(1)} s` : '—'} /><Metric icon={<TerminalSquare size={15} />} label="工具执行累计" value={diagnostics.work.tool_timing_known_attempts ? `${(diagnostics.work.tool_dispatch_ms / 1000).toFixed(1)} s` : '—'} /><Metric icon={<Clock3 size={15} />} label="就绪排队累计" value={diagnostics.work.tool_timing_known_attempts ? `${(diagnostics.work.ready_queue_ms / 1000).toFixed(1)} s` : '—'} /></div>}
    <div className={`trace-workspace graph-first ${showTimeline ? 'with-timeline' : ''}`}>
      <section className="graph-card">
        <div className="card-heading graph-heading"><div><span className="section-kicker">TASK DAG · EXECUTION FLOW</span><h2>{isGroup ? '任务依赖与并行分支' : '任务实际执行路径'}</h2></div>
          <div className="graph-toolbar">
            {isGroup && <div className="graph-view-toggle" role="group" aria-label="链路视图">
              <button className={graphView === 'dependencies' ? 'active' : ''} aria-pressed={graphView === 'dependencies'} onClick={() => { setGraphView('dependencies'); setSelected('plan') }}><GitBranch size={14} />任务依赖</button>
              <button className={graphView === 'expanded' ? 'active' : ''} aria-pressed={graphView === 'expanded'} onClick={() => { setGraphView('expanded'); setSelected('plan') }}><Layers size={14} />展开子图</button>
            </div>}
            <button className={`graph-timeline-toggle ${showTimeline ? 'active' : ''}`} aria-label="事件时间线" aria-expanded={showTimeline} title="事件时间线" onClick={() => setShowTimeline(value => !value)}><List size={16} /></button>
            <span className={`connection-state ${reconnecting ? 'reconnecting' : ''}`}><i />{reconnecting ? '正在重连' : isActive(task?.status) ? '实时事件' : '任务已结束'}</span>
          </div>
        </div>
        {isGroup && <div className="graph-summary"><span>子任务 <b>{branches.length}</b></span><span>无依赖入口 <b>{edges.filter(edge => edge.source === 'plan').length}</b></span><span>结果依赖 <b>{edges.filter(edge => edge.style?.strokeDasharray).length}</b></span><span className="running">执行中 <b>{branches.filter(node => node.data.status === 'running').length}</b></span><span>待输入 <b>{branches.filter(node => node.data.status === 'waiting').length}</b></span><span>待前置 <b>{branches.filter(node => node.data.status === 'blocked').length}</b></span></div>}
        {graph.warnings.map(warning => <div key={warning} className="graph-warning"><CircleAlert size={14} />{warning}</div>)}
        <div className="graph-canvas"><ReactFlow key={`${taskId}:${isGroup ? graphView : domain}`} nodes={nodes.map(node => ({ ...node, selected: node.id === selected }))} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.18, maxZoom: 0.95 }} minZoom={0.08} maxZoom={1.8} nodesDraggable={false} nodesConnectable={false} elementsSelectable onNodeClick={(_, node) => setSelected(node.id)} proOptions={{ hideAttribution: true }}><Background color="#e5ebe9" gap={22} size={1} /><Controls showInteractive={false} />{isGroup && <MiniMap pannable zoomable nodeColor={node => ({ completed: '#74ac91', running: '#38a49b', waiting: '#e8ac56', failed: '#d78686', blocked: '#a8b4c4' } as Record<string, string>)[node.data.status as string] || '#d3dcd7'} />}</ReactFlow></div>
        <div className="graph-legend"><span><i className="legend-dot done" />已完成</span><span><i className="legend-dot active" />执行中</span><span><i className="legend-dot wait" />等待输入</span><span><i className="legend-dot idle" />排队 / 待前置</span><span><i className="legend-dot fail" />失败 / 已取消</span>{isGroup && <span className="dependency-key"><i />结果依赖</span>}</div>
      </section>
      {showTimeline && <section className="event-card"><div className="card-heading event-heading"><div><span className="section-kicker">EXECUTION TRACE</span><h2>事件时间线</h2></div><select value={filter} onChange={e => setFilter(e.target.value)} aria-label="筛选事件"><option value="all">全部事件</option><option value="selected">当前节点</option><option value="node.">图节点</option><option value="tool.">工具调用</option><option value="interrupt.">人工介入</option><option value="memory.">记忆</option><option value="answer.">回答</option></select></div><div className="event-timeline">{visibleEvents.map(event => <EventRow key={event.event_id} event={event} selected={selectedEvents.some(item => item.event_id === event.event_id)} onClick={() => selectEvent(event)} />)}{visibleEvents.length === 0 && <div className="timeline-empty"><CircleDashed size={19} /><span>{isActive(task?.status) ? '等待 Agent 事件…' : '暂无相关执行事件'}</span></div>}</div></section>}
    </div>
    <section className="trace-detail"><div className="detail-title"><div className="detail-icon"><TerminalSquare size={16} /></div><div><span className="section-kicker">SELECTED STEP</span><h2>{selectedNode?.data.title || '执行详情'}</h2></div><span className="detail-count">{selectedEvents.length} 条相关事件</span></div>{selectedDetails && <p className="selected-task-description">{selectedDetails}</p>}<div className="detail-grid">{selectedEvents.slice(-6).map(event => <div className="detail-event" key={event.event_id}><div className="detail-event-top"><span>{eventLabel(event.type)}</span><time>{formatTime(event.created_at)}</time></div><pre>{formatPayload(event)}</pre></div>)}{selectedEvents.length === 0 && <p className="detail-placeholder">{selectedNode?.data.status === 'blocked' ? '前置任务尚未完成，该任务还没有开始执行。' : '暂无相关执行事件。'}</p>}</div></section>
    <section className="checkpoint-card"><div className="checkpoint-heading"><div><span className="section-kicker">PERSISTED STATE</span><h2>状态检查点</h2></div><span>{checkpoints.length} 个 · 可从未完成节点分支回放</span></div><div className="checkpoint-strip">{checkpoints.slice(0, 8).map((point, index) => <div className="checkpoint-item" key={point.checkpoint_id || index}><span className="checkpoint-marker"><Check size={12} /></span><strong>{point.next?.[0] || '任务状态'}</strong><small>步骤 {checkpoints.length - index}</small><code>{String(point.checkpoint_id || '').slice(0, 10)}</code><button className="checkpoint-replay" disabled={!point.next?.length || !task?.session_id} title={point.next?.length ? '从该状态创建回放分支' : '该检查点已结束'} onClick={() => void replay(point.checkpoint_id)}><Play size={12} />分支回放</button></div>)}{checkpoints.length === 0 && <span className="checkpoint-empty">图运行后会在关键状态变化时写入检查点。</span>}</div></section>
    <section className="review-panel"><div className="review-panel-heading"><div><span className="section-kicker">HUMAN REVIEW · FEEDBACK LOOP</span><h2>Bad Case 归因</h2></div><span className={`review-state ${reviewed ? 'saved' : ''}`}>{reviewed ? '已记录人工审核' : '待人工审核'}</span></div><p className="review-helper">标注只记录“问题是什么、为什么发生、期望如何回答”，不会自动修改 Agent；后续可用于 Bad Case 聚类、回归集和训练样本审核。</p><label className="bad-case-toggle"><input type="checkbox" checked={review.is_bad_case} onChange={e => setReview(value => ({ ...value, is_bad_case: e.target.checked }))} /><span><strong>将本轮标记为 Bad Case</strong><small>如用户纠正、表达不满、任务执行错或回复缺少关键信息</small></span></label><div className="review-form-grid"><label>问题分类<select value={review.category} onChange={e => setReview(value => ({ ...value, category: e.target.value }))}><option value="">选择分类</option><option value="intent">意图 / 领域判断</option><option value="slot">槽位与信息澄清</option><option value="memory">上下文 / 记忆</option><option value="knowledge">知识检索</option><option value="skill">Skill 选择或内容</option><option value="tool">工具调用 / 参数</option><option value="policy">安全与权限策略</option><option value="response_quality">回复质量 / 有人感</option><option value="latency">时延与稳定性</option><option value="other">其他</option></select></label><label>根因分析<textarea value={review.root_cause} onChange={e => setReview(value => ({ ...value, root_cause: e.target.value }))} rows={3} placeholder="例如：把“脱妆”直接推断为油皮，缺少肤质确认步骤。" /></label><label>期望行为 / 答案<textarea value={review.expected_answer} onChange={e => setReview(value => ({ ...value, expected_answer: e.target.value }))} rows={3} placeholder="描述更合适的追问、工具选择或回答方式。" /></label><label>补充备注<textarea value={review.note} onChange={e => setReview(value => ({ ...value, note: e.target.value }))} rows={2} placeholder="复现条件、用户后续反馈、归因证据等" /></label></div><div className="review-actions"><span className={reviewMessage && reviewMessage !== '归因已保存' ? 'review-error' : 'review-success'}>{reviewMessage || (reviewed ? '修改尚未保存' : '保存审核结论后，可从执行数据页筛选待归因与 Bad Case')}</span><button className="primary-button small" disabled={savingReview || !task} onClick={() => void saveReview()}>{savingReview ? '保存中…' : <><Save size={14} />保存归因</>}</button></div></section>
  </div>
}

function EventRow({ event, selected, onClick }: { event: RunEvent; selected: boolean; onClick: () => void }) {
  const Icon = event.type.includes('failed') ? CircleAlert : event.type.includes('interrupt') ? ShieldCheck : event.type.startsWith('tool.') ? TerminalSquare : event.type.includes('completed') ? CircleCheck : event.type.startsWith('node.') ? Activity : CornerDownRight
  const title = event.node || event.tool_name || event.reason || event.type
  const description = event.type === 'tool.started' ? JSON.stringify(event.arguments || {}) : event.type === 'tool.completed' ? compact(event.result) : event.elapsed_ms != null ? `${event.stage || event.phase || ''} · ${event.elapsed_ms} ms` : event.question || event.answer || event.error_code || event.status || ''
  return <button className={`event-row ${selected ? 'selected' : ''} ${event.type.includes('failed') ? 'failed' : ''}`} onClick={onClick}><span className={`event-icon ${event.type.startsWith('tool.') ? 'tool' : event.type.includes('interrupt') ? 'wait' : ''}`}><Icon size={14} /></span><span className="event-row-body"><strong>{eventLabel(event.type)}{title && !event.type.startsWith('task.') ? <em>{title}</em> : null}</strong>{description && <small>{description}</small>}</span><time>{formatTime(event.created_at)}</time></button>
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value: string }) { return <div className="trace-metric"><span className="metric-icon">{icon}</span><span><small>{label}</small><strong>{value}</strong></span></div> }
function statusLabel(value?: string) { return ({ queued: '排队中', running: '执行中', waiting_user: '等待用户', completed: '已完成', partial: '部分完成', failed: '执行失败', cancelled: '已取消', interrupted: '已中断' } as Record<string, string>)[value || ''] || '加载中' }
function flowStatus(value: string) { return ({ idle: '待运行', skipped: '无需执行', blocked: '等待前置任务', running: '执行中', waiting: '等待输入', completed: '已完成', partial: '部分完成', failed: '失败', queued: '排队中', cancelled: '已取消', interrupted: '已中断', waiting_user: '等待用户' } as Record<string, string>)[value] || value }
function isActive(value?: string) { return ['queued', 'running', 'waiting_user'].includes(value || '') }
function domainLabel(value: string) { return ({ multi: '多任务', weather: '天气', navigation: '地点与路线', music: '音乐', wellbeing: '健康知识', planning: '行程提醒', local_life: '本地生活', chat: '通用对话' } as Record<string, string>)[value] || value }
function guessDomain(query: string) { return /天气|气温|下雨/.test(query) ? 'weather' : /提醒|行程/.test(query) ? 'planning' : /健康|睡眠|运动|饮食|焦虑/.test(query) ? 'wellbeing' : /导航|去一下|路线|怎么走/.test(query) ? 'navigation' : /歌曲|音乐|播放/.test(query) ? 'music' : 'chat' }
function eventLabel(type: string) { return ({ 'task.queued': '任务已排队', 'task.started': '任务启动', 'task.completed': '任务完成', 'task.failed': '任务失败', 'task.cancelled': '任务取消', 'node.started': '节点开始', 'node.completed': '节点结束', 'tool.started': '工具调用', 'tool.completed': '工具回执', 'interrupt.waiting': '等待用户输入', 'interrupt.required': '需要人工介入', 'memory.loaded': '记忆载入', 'route.completed': '路由完成', 'skill.loaded': 'Skill 加载', 'answer.completed': '回复生成', 'user.resumed': '收到用户操作' } as Record<string, string>)[type] || type }
function formatTime(input: string) { const value = new Date(input); return Number.isNaN(value.getTime()) ? '' : value.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) }
function compact(value: unknown) { const text = JSON.stringify(value) || ''; return text.length > 120 ? `${text.slice(0, 117)}…` : text }
function formatPayload(event: RunEvent) { const { event_id, task_id, seq, created_at, type, ...payload } = event; return JSON.stringify(payload, null, 2) }
