import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { Activity, Bell, ChevronRight, CircleHelp, GitBranch, HeartPulse, ListTodo, MapPin, MessageSquareText, Plus, RefreshCw, Settings2, ShieldCheck, Sparkles, Utensils, Volume2, Wind, X } from 'lucide-react'
import { api, type Health, type Interrupt, type RouteReceipt, type RunEvent, type Session, type Task, messageText } from './api'
import RoutePage, { RoutePreview } from './RoutePreview'
import { messageTaskId, withoutStandaloneRouteLinks } from './routeDisplay'
import TracePage from './TracePage'
import RunDataPage from './RunDataPage'
import LifePage from './LifePage'
import DiscoverPage from './DiscoverPage'
import { taskEventStatus } from './taskEvents'
import { interruptExpired } from './freshness'
import { acceptStreamSequence, applyReplyEvent } from './replyStream'
import './TaskResults.css'

function App() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(localStorage.getItem('lifeplus-session'))
  const [lifeState, setLifeState] = useState({ itineraries: [] as any[], reminders: [] as any[] })
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState('')
  const [unreadNotifications, setUnreadNotifications] = useState(0)
  const location = useLocation()
  const navigate = useNavigate()
  const pageLabel = location.pathname.startsWith('/routes/') ? '出行路线' : location.pathname.startsWith('/discover') ? '本地生活' : location.pathname.startsWith('/life') ? '行程与提醒' : location.pathname.startsWith('/runs/') ? '执行链路详情' : location.pathname.startsWith('/runs') ? '执行数据' : '对话交互'

  async function refresh() {
    try {
      const [sessions, nextLifeState, serviceHealth] = await Promise.all([api.sessions(), api.lifeState(), api.health()])
      setSessions(sessions); setLifeState(nextLifeState); setHealth(serviceHealth); setError('')
    }
    catch (e) { setError(e instanceof Error ? e.message : '服务连接失败') }
  }
  useEffect(() => { void refresh() }, [])
  useEffect(() => {
    const update = () => api.notifications().then(items => setUnreadNotifications(items.filter(item => !item.read_at).length)).catch(() => undefined)
    void update()
    const timer = window.setInterval(() => void update(), 3000)
    return () => window.clearInterval(timer)
  }, [])

  async function createSession() {
    try {
      const session = await api.createSession()
      setSessions(current => [session, ...current]); setActiveSession(session.id)
      localStorage.setItem('lifeplus-session', session.id)
      navigate(`/chat/${session.id}`)
    } catch (e) { setError(e instanceof Error ? e.message : '无法创建会话') }
  }

  useEffect(() => {
    if (location.pathname === '/' && activeSession) navigate(`/chat/${activeSession}`, { replace: true })
  }, [location.pathname, activeSession, navigate])

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><HeartPulse size={19} /></div><div><strong>LIFEPLUS</strong><span>AGENT HARNESS</span></div></div>
      <div className="workspace-label">工作台</div>
      <nav className="main-nav">
        <NavLink to={activeSession ? `/chat/${activeSession}` : '/'} className={({ isActive }) => `nav-item ${isActive || location.pathname.startsWith('/chat') ? 'active' : ''}`}><MessageSquareText size={16} /> 对话交互</NavLink>
        <NavLink to="/runs" className={({ isActive }) => `nav-item ${isActive || location.pathname.startsWith('/runs') ? 'active' : ''}`}><GitBranch size={16} /> 执行数据</NavLink>
        <NavLink to="/discover" title="本地生活" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><Utensils size={16} /> 本地生活</NavLink>
        <NavLink to="/life" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><Bell size={16} /> 行程与提醒{unreadNotifications > 0 && <span aria-label={`${unreadNotifications}条未读提醒`}> ({unreadNotifications})</span>}</NavLink>
      </nav>
      <div className="session-heading"><span>最近会话</span><button className="icon-button subtle" onClick={createSession} title="新建会话"><Plus size={16} /></button></div>
      <div className="session-list">{sessions.map(item => <button key={item.id} onClick={() => { setActiveSession(item.id); localStorage.setItem('lifeplus-session', item.id); navigate(`/chat/${item.id}`) }} className={`session-row ${activeSession === item.id ? 'selected' : ''}`}><span className="session-dot" />{item.title || '新对话'}</button>)}</div>
      <div className="sidebar-bottom"><div className="vehicle-mini"><div className="vehicle-mini-head"><span className="vehicle-light" />生活任务</div><div className="vehicle-stat"><ListTodo size={14} /><span>行程</span><strong>{lifeState.itineraries.length}</strong></div><div className="vehicle-stat"><Bell size={14} /><span>提醒</span><strong>{lifeState.reminders.length}</strong></div><div className="vehicle-mode"><ShieldCheck size={13} />状态可追踪</div></div><div className="profile"><div className="profile-avatar">侯</div><div><strong>本地演示用户</strong><span>LifePlus Workspace</span></div><Settings2 size={15} className="profile-settings" /></div></div>
    </aside>
    <main className="main-area"><header className="topbar"><div className="breadcrumb"><span>LifePlus Agent Harness</span><ChevronRight size={14} /><strong>{pageLabel}</strong></div><div className="topbar-right"><span className="service-state"><i />服务已连接</span><span className="top-divider" /><button className="icon-button" title="帮助"><CircleHelp size={17} /></button></div></header>
      {error && <div className="global-error"><Activity size={15} />{error}<button onClick={refresh}>重试</button></div>}
      <Routes><Route path="/" element={<Welcome onCreate={createSession} />} /><Route path="/chat/:sessionId" element={<ChatPage onTask={refresh} health={health} />} /><Route path="/routes/:taskId" element={<RoutePage />} /><Route path="/runs" element={<RunDataPage />} /><Route path="/runs/:taskId" element={<TracePage />} /><Route path="/life" element={<LifePage />} /><Route path="/discover" element={<DiscoverPage />} /><Route path="*" element={<Welcome onCreate={createSession} />} /></Routes>
    </main>
  </div>
}

function Welcome({ onCreate }: { onCreate: () => void }) {
  return <section className="welcome"><div className="welcome-icon"><Sparkles size={22} /></div><p className="eyebrow">LIFE SERVICE AGENT RUNTIME</p><h1>让生活任务<br /><span>清晰、可控、可追踪</span></h1><p className="welcome-copy">查询天气与路线，检索音乐和健康知识，创建行程与提醒，并查看 Agent 的完整执行链路。</p><button className="primary-button" onClick={onCreate}><Plus size={16} />开始新对话</button><div className="welcome-domains"><span><Wind size={14} />天气</span><span><MapPin size={14} />出行</span><span><Volume2 size={14} />音乐</span><span><HeartPulse size={14} />健康</span><span><Bell size={14} />提醒</span></div></section>
}

function ChatPage({ onTask, health }: { onTask: () => void; health: Health | null }) {
  const { sessionId = '' } = useParams()
  const [history, setHistory] = useState<any[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [routes, setRoutes] = useState<RouteReceipt[]>([])
  const [routeSelections, setRouteSelections] = useState<Record<string, RouteReceipt>>({})
  const [routeLoadError, setRouteLoadError] = useState('')
  const [active, setActive] = useState<Task | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [streamed, setStreamed] = useState<Record<string, string>>({})
  const [text, setText] = useState('')
  const [mode, setMode] = useState(localStorage.getItem('lifeplus-mode') || 'live')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [memories, setMemories] = useState<any[]>([])
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [memoryKey, setMemoryKey] = useState('通勤目的地')
  const [memoryValue, setMemoryValue] = useState('')
  const currentSession = useRef(sessionId)
  currentSession.current = sessionId
  const conversation = useRef<HTMLDivElement>(null)
  const followOutput = useRef(true)
  const navigate = useNavigate()

  async function refreshHistory() {
    const [messages, allTasks, routeResult] = await Promise.all([api.messages(sessionId), api.tasks(sessionId),
      api.sessionRoutes(sessionId).then(value => ({ value, error: '' })).catch(e => ({ value: null, error: e instanceof Error ? e.message : '路线选择加载失败' }))])
    if (currentSession.current !== sessionId) return
    setHistory(messages); setTasks(allTasks)
    if (routeResult.value) setRoutes(routeResult.value)
    setRouteLoadError(routeResult.error)
    setActive(current => allTasks.find(task => task.id === current?.id)
      || allTasks.find(task => ['running', 'queued', 'waiting_user'].includes(task.status)) || allTasks[0] || null)
  }

  useEffect(() => {
    let live = true
    setHistory([]); setTasks([]); setRoutes([]); setRouteSelections({}); setRouteLoadError(''); setActive(null); setEvents([]); setStreamed({}); followOutput.current = true
    void refreshHistory().catch(e => live && setError(e.message))
    const timer = window.setInterval(() => { if (live) void refreshHistory().catch(() => undefined) }, 1200)
    return () => { live = false; window.clearInterval(timer) }
  }, [sessionId])

  function updateRoot(id: string, values: Partial<Task>) {
    setTasks(current => current.map(task => task.id === id ? { ...task, ...values } : task))
    setActive(current => current?.id === id ? { ...current, ...values } : current)
  }

  function receiveEvent(rootId: string, event: RunEvent) {
      setEvents(current => current.some(item => item.event_id === event.event_id) ? current : [...current, event])
      setStreamed(current => applyReplyEvent(current, event))
      if (event.type === 'group.updated') {
        updateRoot(rootId, { status: event.status, children: event.children })
      }
      if (event.type.startsWith('task.')) {
        const status = taskEventStatus(event.type)
        updateRoot(rootId, { ...(status ? { status } : {}), ...(event.answer ? { answer: event.answer } : {}) })
        if (event.type === 'task.failed' && event.error) setError(event.error)
        if (['task.completed', 'task.partial', 'task.failed', 'task.cancelled'].includes(event.type)) {
          setBusy(false); void refreshHistory().catch(() => undefined); onTask()
        }
      }
      if (event.type === 'interrupt.required' || event.type === 'subtask.interrupt.required') {
        setBusy(false)
        void api.task(rootId).then(task => { if (task.session_id === currentSession.current) updateRoot(rootId, task) }).catch(() => undefined)
      }
  }

  useEffect(() => {
    if (followOutput.current && conversation.current) conversation.current.scrollTop = conversation.current.scrollHeight
  }, [streamed, history.length, events.length, routes.length])

  useEffect(() => { void api.memories(sessionId).then(setMemories).catch(() => undefined) }, [sessionId, memoryOpen])

  async function send(value = text) {
    if (!value.trim() || busy) return
    setError(''); setBusy(true); setText('')
    try {
      const task = await api.submit(sessionId, value.trim(), mode)
      if (task.additional_task_error) setError(task.additional_task_error.message)
      followOutput.current = true
      setActive({ id: task.task_id, session_id: sessionId, query: value.trim(), status: task.status, mode, last_seq: 0, created_at: new Date().toISOString() })
      setHistory(current => [...current, { role: 'human', content: value.trim() }])
      setBusy(false)
      onTask()
    } catch (e) { setBusy(false); setText(current => current || value); setError(e instanceof Error ? e.message : '发送失败') }
  }

  async function resume(taskId: string, interrupt: Interrupt, action: string, value: Record<string, unknown> = {}) {
    setError(''); setBusy(true)
    try {
      const result = await api.resume(taskId, interrupt, action, value)
      if (result.additional_task_error) setError(result.additional_task_error.message)
      await refreshHistory()
      if (result.created_task_id) {
        setActive(await api.task(result.created_task_id))
      }
      setBusy(false)
      onTask()
    } catch (e) { setBusy(false); setError(e instanceof Error ? e.message : '恢复失败') }
  }

  async function saveMemory() {
    try { await api.putMemory(sessionId, memoryKey, memoryValue); setMemoryValue(''); setMemories(await api.memories(sessionId)) }
    catch (e) { setError(e instanceof Error ? e.message : '记忆保存失败') }
  }

  async function refreshWaiting(task: Task) {
    if (!task.interrupt || busy) return
    setBusy(true); setError('')
    try { await api.refreshTask(task.id, task.interrupt); await refreshHistory() }
    catch (e) { setError(e instanceof Error ? e.message : '刷新任务失败') }
    finally { setBusy(false) }
  }

  const normalizedHistory = history.filter((m: any) => ['human', 'user', 'ai', 'assistant'].includes(m.type || m.role)).map((m: any) => ({ role: ['human', 'user'].includes(m.type || m.role) ? 'user' : 'assistant', content: messageText(m.content), taskId: messageTaskId(m.id) }))
  const displayed = [...normalizedHistory,
    ...events.filter(e => e.type === 'answer.completed' && !normalizedHistory.some(m => m.role === 'assistant' && m.content === e.answer)).map(e => ({ role: 'assistant', content: e.answer, taskId: e.child_task_id || e.task_id })),
    ...(active?.status === 'failed' && active.answer && !normalizedHistory.some(m => m.role === 'assistant' && m.content === active.answer) && !events.some(e => e.type === 'answer.completed' && e.answer === active.answer) ? [{ role: 'assistant', content: active.answer, taskId: active.id }] : [])]
  const interrupt = active?.interrupt
  const rootTasks = [...tasks.filter(task => task.session_id === sessionId && task.id !== active?.id), ...(active?.session_id === sessionId ? [active] : [])]
  const waitingTasks = rootTasks.flatMap(task => task.children || [task]).filter(task => task.status === 'waiting_user' && task.interrupt)
  const activeEvents = events.filter(event => event.task_id === active?.id)
  const currentNode = [...activeEvents].reverse().find(event => event.type === 'node.started')?.node
  const recentEvent = [...activeEvents].reverse().find(event => event.type === 'node.started' || event.type === 'tool.started')
  function selectRoute(sourceJob: string, selected: RouteReceipt) {
    setRouteSelections(current => ({ ...current, [sourceJob]: selected }))
  }

  return <div className="chat-layout">
    {rootTasks.map(task => <TaskEventStream key={`${sessionId}:${task.id}`} task={task} onEvent={event => receiveEvent(task.id, event)} onError={message => setError(message)} />)}
    <section className="chat-column"><div className="chat-heading"><div><div className="title-row"><h1>LifePlus 生活助理</h1><span className={`mode-pill ${mode}`}>{mode === 'demo' ? '离线演示' : '真实模型'}</span></div><p>{mode === 'live' ? (health?.model_configured ? `模型已配置：${health.model_name || '服务端模型'} · 工具队列按需调度` : '模型未配置 · 添加服务端 API Key 后即可发起真实推理') : '离线模拟 · 使用本地演示数据'}</p></div><div className="heading-actions"><label className="mode-control"><span>运行模式</span><select value={mode} onChange={e => { setMode(e.target.value); localStorage.setItem('lifeplus-mode', e.target.value) }} disabled={busy}><option value="live">真实模型</option><option value="demo">离线演示</option></select></label><button className="secondary-button" onClick={() => setMemoryOpen(true)}><Sparkles size={15} />记忆</button></div></div>
      <div className="conversation-scroll" id="conversation-scroll" ref={conversation} onScroll={() => { const element = conversation.current; if (element) followOutput.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100 }}>
        {routeLoadError && <div className="route-selection-load-error" role="alert"><CircleHelp size={14} /><span>路线选择加载失败：{routeLoadError}</span><button className="secondary-button small" onClick={() => void refreshHistory().catch(e => setError(e.message))}><RefreshCw size={13} />重试</button></div>}
        {displayed.length === 0 && !active && <div className="chat-empty"><div className="chat-empty-mark"><HeartPulse size={21} /></div><h2>你好，今天想处理什么？</h2><p>可以连续说，也可以随时补充信息</p><div className="suggestions"><button onClick={() => void send('杭州明天的天气怎么样')}>查杭州明天天气</button><button onClick={() => void send('从西湖到杭州东站怎么走')}>规划出行路线</button><button onClick={() => void send('搜索周杰伦的歌曲')}>找一首歌</button><button onClick={() => void send('提醒我明天上午9点带伞')}>创建生活提醒</button></div></div>}
        {displayed.map((message, index) => { const attached = message.role === 'assistant' ? routes.filter(route => route.root_task_id === message.taskId || route.task_id === message.taskId) : []; return <div key={`${message.taskId || index}-${message.role}`} className={`message-row ${message.role}`}><div className={`message-avatar ${message.role}`}>{message.role === 'user' ? '我' : <Sparkles size={14} />}</div><div className={`message-content ${attached.length ? 'with-route' : ''}`}><div className="message-author">{message.role === 'user' ? '你' : 'LifePlus'}<time>{message.role === 'assistant' && active?.mode === 'demo' ? '模拟结果' : ''}</time></div><div className={`message-bubble ${message.role}`}><MessageText content={attached.length ? withoutStandaloneRouteLinks(message.content) : message.content} /></div>{attached.map(route => <RoutePreview key={route.job_id} route={routeSelections[route.job_id] || route} onRouteChange={selected => selectRoute(route.job_id, selected)} />)}</div></div> })}
        {active && ['queued', 'running'].includes(active.status) && <div className="live-progress"><span className="pulse-dot" /><div><strong>{recentEvent?.type === 'tool.started' ? `正在调用 ${recentEvent.tool_name}` : `正在执行${currentNode ? ` · ${currentNode}` : ''}`}</strong><span>{recentEvent?.type === 'tool.started' ? '等待工具返回结果' : '正在推进任务节点'}</span></div><button className="icon-button" title="取消任务" onClick={() => void api.cancel(active.id).then(() => setBusy(false))}><X size={16} /></button></div>}
        {rootTasks.filter(task => ['queued', 'running', 'waiting_user'].includes(task.status)).map(task => <TaskResults key={task.id} task={task} streamed={streamed} />)}
        {routes.filter(route => !displayed.some(message => message.role === 'assistant' && [route.root_task_id, route.task_id].includes(message.taskId))).map(route => <div className="message-row assistant" key={route.job_id}><div className="message-avatar assistant"><Sparkles size={14} /></div><div className="message-content with-route"><div className="message-author">LifePlus</div><RoutePreview route={routeSelections[route.job_id] || route} onRouteChange={selected => selectRoute(route.job_id, selected)} /></div></div>)}
        {waitingTasks.map(task => <div key={task.id} className="task-clarification"><strong>{task.query}</strong><InterruptCard key={task.interrupt!.id} interrupt={task.interrupt!} busy={busy} onRefresh={() => void refreshWaiting(task)} onResume={(action, value) => void resume(task.id, task.interrupt!, action, value)} /></div>)}
        {error && <div className="inline-error"><CircleHelp size={15} />{error}<button onClick={() => setError('')}><X size={14} /></button></div>}
      </div>
      {active && <button className="run-preview" onClick={() => navigate(`/runs/${active.id}`)}><span className="run-preview-icon"><GitBranch size={15} /></span><span><strong>{active.status === 'waiting_user' ? 'Agent 正在等待你的输入' : active.status === 'completed' ? '任务已完成' : '查看 Agent 执行链路'}</strong><small>{recentEvent?.node || recentEvent?.tool_name || active.status} · {events.length} 条事件</small></span><ChevronRight size={16} /></button>}
      <form className="composer" onSubmit={e => { e.preventDefault(); void send() }}><textarea value={text} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} placeholder="说说你想做什么…" disabled={busy} rows={2} /><div className="composer-bottom"><span><span className="keyboard-key">↵</span>发送 <span className="keyboard-key">⇧ ↵</span>换行</span><button className="send-button" type="submit" disabled={busy || !text.trim()}>{busy ? <span className="mini-spinner" /> : <ChevronRight size={17} />}</button></div></form>
    </section>
    <aside className="chat-side"><div className="side-panel-head"><div><span className="section-kicker">LIVE TASK</span><h3>当前任务</h3></div><Activity size={16} /></div>
      {active ? <><div className="task-summary"><span className={`status-badge ${active.status}`}><i />{statusLabel(active.status)}</span><p>{active.query}</p><div className="summary-meta"><span>{active.mode === 'demo' ? '模拟工具' : '真实工具'}</span><span>·</span><span>{new Date(active.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span></div></div><div className="current-step"><span className="step-label">执行位置</span><div className="step-value"><span className="step-icon"><Activity size={15} /></span><div><strong>{currentNode || (interrupt ? '等待用户' : '任务处理中')}</strong><small>{recentEvent?.type === 'tool.started' ? `工具 · ${recentEvent.tool_name}` : interrupt?.question || '状态由运行时事件更新'}</small></div></div></div><Link to={`/runs/${active.id}`} className="side-link">打开完整执行链路<ChevronRight size={15} /></Link></> : <div className="side-empty"><Activity size={17} /><span>发起任务后显示实时状态</span></div>}
      <div className="side-divider" /><div className="side-panel-head"><div><span className="section-kicker">RUNTIME</span><h3>运行时后端</h3></div></div><div className="device-rows"><div><Activity size={15} /><span>Checkpoint</span><strong>{health?.checkpoint_backend || 'sqlite'}</strong></div><div><Sparkles size={15} /><span>会话缓存</span><strong>{health?.session_cache || 'local'}</strong></div><div><ListTodo size={15} /><span>长期记忆</span><strong>{health?.long_term_memory || 'sqlite'}</strong></div><div><HeartPulse size={15} /><span>知识检索</span><strong>{health?.rag_backend || 'chroma'}</strong></div></div><div className="side-divider" /><div className="side-panel-head"><div><span className="section-kicker">RECENT RUNS</span><h3>最近执行</h3></div></div><div className="recent-runs">{tasks.slice(0, 5).map(task => <Link key={task.id} to={`/runs/${task.id}`} className="recent-run"><span className={`run-dot ${task.status}`} /><span><strong>{task.query}</strong><small>{statusLabel(task.status)}</small></span><ChevronRight size={14} /></Link>)}{tasks.length === 0 && <div className="runs-placeholder">完成任务后会保存在这里</div>}</div>
    </aside>
    {memoryOpen && <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setMemoryOpen(false) }}><section className="memory-modal"><div className="modal-head"><div><span className="section-kicker">LONG-TERM MEMORY</span><h2>用户偏好</h2></div><button className="icon-button" onClick={() => setMemoryOpen(false)}><X size={17} /></button></div><p className="modal-copy">偏好保存在当前演示用户下，后续会话可读取。</p><div className="memory-editor"><input value={memoryKey} onChange={e => setMemoryKey(e.target.value)} placeholder="偏好名称" /><input value={memoryValue} onChange={e => setMemoryValue(e.target.value)} placeholder="例如：常用目的地是公司" /><button className="primary-button small" onClick={() => void saveMemory()} disabled={!memoryValue.trim()}>保存偏好</button></div><div className="memory-list">{memories.map(memory => <div className="memory-item" key={memory.key}><div><strong>{memory.key}</strong><span>{memory.value}</span></div><button className="icon-button" title="删除偏好" onClick={() => void api.deleteMemory(sessionId, memory.key).then(() => setMemories(current => current.filter(row => row.key !== memory.key)))}><X size={15} /></button></div>)}{memories.length === 0 && <div className="runs-placeholder">还没有保存的偏好</div>}</div></section></div>}
  </div>
}

function InterruptCard({ interrupt, busy, onResume, onRefresh }: { interrupt: Interrupt; busy: boolean; onRefresh: () => void; onResume: (action: string, value: Record<string, unknown>) => void }) {
  const [answer, setAnswer] = useState('')
  const [now, setNow] = useState(Date.now())
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer) }, [])
  if (interruptExpired(interrupt, now)) return <div className="interrupt-card"><p>{interrupt.kind === 'approval' ? '本次操作确认已过期' : '本次候选或待补充信息已过期'}</p><button className="secondary-button" disabled={busy} onClick={onRefresh}><RefreshCw size={15} />刷新当前任务</button>{interrupt.kind === 'approval' && <button className="secondary-button" disabled={busy} onClick={() => onResume('reject', {})}>拒绝</button>}</div>
  return <div className="interrupt-card"><div className="interrupt-title"><span className="waiting-mark">{interrupt.kind === 'approval' ? <ShieldCheck size={16} /> : interrupt.kind === 'selection' ? <Settings2 size={16} /> : <CircleHelp size={16} />}</span><div><strong>{interrupt.kind === 'approval' ? '请确认操作' : interrupt.kind === 'selection' ? '选择一个结果' : '还需要一点信息'}</strong><p>{interrupt.question}</p></div></div>
    {interrupt.arguments && <pre className="action-args">{JSON.stringify(interrupt.arguments, null, 2)}</pre>}
    {interrupt.candidates && <div className="candidate-list">{interrupt.candidates.map((item, index) => <button disabled={busy} key={String(item.id || index)} onClick={() => onResume('answer', { index: index + 1 })}><span className="candidate-index">{index + 1}</span><span><strong>{String(item.name || item.title || '候选结果')}</strong><small>{String(item.address || item.singer || item.album || '')}</small></span><ChevronRight size={15} /></button>)}</div>}
    {interrupt.kind === 'approval' && <div className="interrupt-actions"><button disabled={busy} className="secondary-button" onClick={() => onResume('reject', {})}>拒绝</button><button disabled={busy} className="primary-button small" onClick={() => onResume('approve', {})}><ShieldCheck size={15} />确认执行</button></div>}
    {['clarification', 'selection'].includes(interrupt.kind) && <form className="clarify-form" onSubmit={e => { e.preventDefault(); if (answer.trim()) onResume('answer', { text: answer.trim() }) }}><input value={answer} onChange={e => setAnswer(e.target.value)} placeholder={interrupt.kind === 'selection' ? '输入序号或名称' : interrupt.field === 'city' ? '输入城市名称' : '补充信息'} /><button className="primary-button small" disabled={busy || !answer.trim()} type="submit">继续</button></form>}
  </div>
}

function TaskEventStream({ task, onEvent, onError }: { task: Task; onEvent: (event: RunEvent) => void; onError: (message: string) => void }) {
  const cursors = useRef<Record<string, number>>({})
  const callbacks = useRef({ onEvent, onError })
  callbacks.current = { onEvent, onError }
  const enabled = ['queued', 'running', 'waiting_user'].includes(task.status)
  useEffect(() => {
    if (!enabled) return
    return api.subscribe(task.id, cursors.current[task.id] || 0, event => {
      if (acceptStreamSequence(cursors.current, task.id, event.seq)) callbacks.current.onEvent(event)
    }, error => callbacks.current.onError(error.message))
  }, [task.id, enabled])
  return null
}

function TaskResults({ task, streamed }: { task: Task; streamed: Record<string, string> }) {
  if (!task.children?.length) return streamed[task.id] ? <div className="message-row assistant"><div className="message-avatar assistant"><Sparkles size={14} /></div><div className="message-content"><div className="message-author">LifePlus</div><div className="message-bubble assistant" aria-live="polite"><MessageText content={streamed[task.id]} /><span className="stream-caret" /></div></div></div> : null
  return <section className="task-results"><div className="task-results-heading"><GitBranch size={16} /><strong>{task.children.filter(child => child.status === 'completed').length} / {task.children.length} 项任务已完成</strong></div>
    {task.children.map(child => <article className="task-result" key={child.id}><header><Link to={`/runs/${child.id}`}>{child.query}</Link><span>{statusLabel(child.status)}</span><button className="icon-button" title="取消子任务" disabled={['completed', 'failed', 'cancelled'].includes(child.status)} onClick={() => void api.cancel(child.id)}><X size={14} /></button></header>{(child.answer || streamed[child.id]) && <p aria-live="polite"><MessageText content={child.answer || streamed[child.id]} />{child.status === 'running' && <span className="stream-caret" />}</p>}</article>)}
  </section>
}

function MessageText({ content }: { content: string }) {
  return <>{content.split(/(https?:\/\/[^\s<>，。！？）)]+)/g).map((part, index) => /^https?:\/\//.test(part)
    ? <a key={index} href={part} target="_blank" rel="noreferrer" style={{ color: '#21775d', textDecoration: 'underline' }}>{part}</a>
    : part)}</>
}

function statusLabel(status: string) { return ({ queued: '排队中', blocked: '等待前置任务', running: '执行中', waiting_user: '等待输入', completed: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' } as Record<string, string>)[status] || status }

export default App
