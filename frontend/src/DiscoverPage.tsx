import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { RoutePreview } from './RoutePreview'
import type { RouteReceipt } from './api'
import { ArrowUpRight, Check, CircleAlert, Coffee, Film, GitBranch, LoaderCircle, MapPin, MicVocal, Navigation, RefreshCw, Search, Star, Utensils, X } from 'lucide-react'
import { api, type Task } from './api'
import { categoryNames, distanceText, venueParameters, type Category, type LocalFilters, type Venue } from './localLife'
import './DiscoverPage.css'
import { interruptExpired } from './freshness'

const icons = { restaurant: Utensils, ktv: MicVocal, cinema: Film, cafe: Coffee }
const terminal = ['completed', 'partial', 'failed', 'cancelled', 'interrupted']

export default function DiscoverPage() {
  const [params, setParams] = useSearchParams()
  const linkedTask = params.get('task')
  const [filters, setFilters] = useState<LocalFilters>({ category: 'restaurant', anchor: '东南大学无锡国际校区', city: '无锡市', radius_m: 3000, keyword: '', sort_by: 'distance' })
  const [session, setSession] = useState('')
  const [task, setTask] = useState<Task | null>(null)
  const [items, setItems] = useState<Venue[]>([])
  const [selected, setSelected] = useState<Venue | null>(null)
  const [searchInfo, setSearchInfo] = useState<any>(null)
  const [route, setRoute] = useState<any>(null)
  const [routeReceipt, setRouteReceipt] = useState<RouteReceipt | null>(null)
  const [mode, setMode] = useState('driving')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [clarification, setClarification] = useState('')
  const [reply, setReply] = useState('')
  const lock = useRef(false)

  useEffect(() => {
    if (!linkedTask) return
    let live = true
    void api.task(linkedTask).then(fresh => {
      if (!live) return
      setTask(fresh); setSession(fresh.session_id)
      const spec = JSON.parse(fresh.plan_json || '{}')
      if (spec.input_request?.action === 'local_search') setFilters(current => ({ ...current, ...spec.input_request.parameters }))
    }).catch(e => { if (live) setError(e.message) })
    return () => { live = false }
  }, [linkedTask])

  useEffect(() => {
    if (!task?.id) return
    let live = true
    let sequence = 0
    setReply('')
    const stop = api.subscribe(task.id, 0, event => {
      if (!live || event.seq <= sequence) return
      sequence = event.seq
      if (event.type === 'answer.started') setReply('')
      if (event.type === 'answer.delta') setReply(current => current + event.delta)
      if (['answer.replaced', 'answer.completed'].includes(event.type)) setReply(event.answer || '')
    }, () => undefined)
    return () => { live = false; stop() }
  }, [task?.id])

  useEffect(() => {
    if (!task?.id) return
    let live = true
    let fetching = false
    const taskId = task.id
    async function tick() {
      if (fetching) return
      fetching = true
      try {
        const [fresh, events] = await Promise.all([api.task(taskId), api.trace(taskId)])
        if (!live) return
        setTask(fresh)
        const search = [...events].reverse().find(event => event.type === 'tool.completed' && event.tool_name === 'maps_local_search')?.result
        const detail = [...events].reverse().find(event => event.type === 'tool.completed' && event.tool_name === 'maps_place_detail')?.result?.item
        const latestRoute = [...events].reverse().find(event => event.type === 'tool.completed' && event.tool_name === 'maps_route')?.result
        if (search) { setItems(search.items || []); setSearchInfo(search) }
        if (detail) setSelected(detail)
        if (latestRoute) {
          setRoute(latestRoute)
          const receipt = await api.route(taskId)
          if (live) setRouteReceipt(receipt)
        }
        if (fresh.status === 'failed') setError(fresh.answer || '查询失败，请重试')
        if (terminal.includes(fresh.status)) window.clearInterval(timer)
      } catch (e) { if (live) setError(e instanceof Error ? e.message : '无法读取查询状态') }
      finally { fetching = false }
    }
    const timer = window.setInterval(() => void tick(), 700)
    void tick()
    return () => { live = false; window.clearInterval(timer) }
  }, [task?.id])

  async function submit(action: 'local_search' | 'local_detail' | 'local_route', parameters: Record<string, unknown>, reset = false) {
    if (lock.current) return
    lock.current = true; setBusy(true); setError('')
    try {
      if (task && !terminal.includes(task.status)) await api.cancel(task.id).catch((e: Error) => { if (!e.message.includes('已经结束')) throw e })
      const sessionId = session || (await api.createSession()).id
      setSession(sessionId)
      const response = await api.localTask(sessionId, action, parameters)
      if (reset) { setItems([]); setSelected(null); setSearchInfo(null) }
      setRoute(null)
      setRouteReceipt(null)
      setTask(await api.task(response.task_id))
      setParams({ task: response.task_id })
    } catch (e) { setError(e instanceof Error ? e.message : '提交失败') }
    finally { lock.current = false; setBusy(false) }
  }

  async function respond(value: Record<string, unknown>) {
    if (!task?.interrupt || lock.current) return
    lock.current = true; setBusy(true); setError('')
    try { await api.resume(task.id, task.interrupt, 'answer', value); setTask(await api.task(task.id)); setClarification('') }
    catch (e) { setError(e instanceof Error ? e.message : '选择失败') }
    finally { lock.current = false; setBusy(false) }
  }
  const choose = (index: number) => respond({ index })

  async function refreshWaiting() {
    if (!task?.interrupt || lock.current) return
    lock.current = true; setBusy(true); setError('')
    try {
      await api.refreshTask(task.id, task.interrupt)
      setItems([]); setSelected(null); setRoute(null); setSearchInfo(null); setClarification('')
      setTask(await api.task(task.id))
    } catch (e) { setError(e instanceof Error ? e.message : '刷新任务失败') }
    finally { lock.current = false; setBusy(false) }
  }

  const active = task && !terminal.includes(task.status)
  const waitingExpired = !!(task?.status === 'waiting_user' && task.interrupt && interruptExpired(task.interrupt))
  const choosingVenue = task?.interrupt?.kind === 'selection' && task.interrupt.field !== 'anchor'
  const Icon = icons[filters.category]
  return <div className="discover-page">
    <header className="discover-header"><div><h1>本地生活</h1><p>{categoryNames[filters.category]} · {filters.city || '指定地点周边'}</p></div>{task && <Link to={`/runs/${task.id}`} className="secondary-button"><GitBranch size={15} />执行链路</Link>}</header>
    <form className="discover-filters" onSubmit={e => { e.preventDefault(); void submit('local_search', { ...filters }, true) }}>
      <div className="discover-categories" role="group" aria-label="商家分类">{(Object.keys(categoryNames) as Category[]).map(category => {
        const CategoryIcon = icons[category]
        return <button key={category} type="button" className={filters.category === category ? 'selected' : ''} aria-pressed={filters.category === category} onClick={() => setFilters(current => ({ ...current, category }))}><CategoryIcon size={17} />{categoryNames[category]}</button>
      })}</div>
      <div className="discover-fields"><label>城市<input value={filters.city} onChange={e => setFilters({ ...filters, city: e.target.value })} /></label><label className="anchor-field">参考地点<input required value={filters.anchor} onChange={e => setFilters({ ...filters, anchor: e.target.value })} /><small>默认参考点，非实时定位</small></label><label>范围<select value={filters.radius_m} onChange={e => setFilters({ ...filters, radius_m: Number(e.target.value) })}><option value={1000}>1 公里</option><option value={3000}>3 公里</option><option value={5000}>5 公里</option><option value={10000}>10 公里</option></select></label><label>关键词<input placeholder="菜系、品牌或店名" value={filters.keyword} onChange={e => setFilters({ ...filters, keyword: e.target.value })} /></label></div>
      <div className="discover-fields secondary"><label>排序<select value={filters.sort_by} onChange={e => setFilters({ ...filters, sort_by: e.target.value as 'distance' | 'rating' })}><option value="distance">距离优先</option><option value="rating">本批评分优先</option></select></label><label>最低评分<input type="number" min="0" max="5" step="0.1" placeholder="不限" value={filters.min_rating ?? ''} onChange={e => setFilters({ ...filters, min_rating: e.target.value === '' ? undefined : Number(e.target.value) })} /></label><label>参考人均上限（元）<input type="number" min="1" max="10000" placeholder="不限" value={filters.max_cost ?? ''} onChange={e => setFilters({ ...filters, max_cost: e.target.value === '' ? undefined : Number(e.target.value) })} /></label><button type="submit" className="primary-button" disabled={busy || !filters.anchor.trim()}>{busy ? <LoaderCircle size={16} className="spin" /> : <Search size={16} />}搜索</button></div>
    </form>
    {error && <div role="alert" className="inline-error"><CircleAlert size={16} />{error}</div>}
    {active && <div className="discover-progress"><LoaderCircle size={15} />{task.status === 'waiting_user' ? task.interrupt?.question : '正在查询真实服务…'}<button className="icon-button" title="取消查询" onClick={() => void api.cancel(task.id).then(() => api.task(task.id)).then(setTask).catch(e => setError(e.message))}><X size={15} /></button></div>}
    {waitingExpired && <div className="discover-progress"><span>本次候选已过期</span><button className="secondary-button" disabled={busy} onClick={() => void refreshWaiting()}><RefreshCw size={15} />刷新当前任务</button></div>}
    {!waitingExpired && task?.interrupt?.field === 'anchor' && <div className="discover-anchor-choices">{task.interrupt.candidates?.map((candidate, index) => <button className="secondary-button" key={index} disabled={busy} onClick={() => void choose(index + 1)}><MapPin size={14} />{String(candidate.name)}</button>)}</div>}
    {!waitingExpired && task?.status === 'waiting_user' && task.interrupt?.kind === 'clarification' && <form className="clarify-form" onSubmit={e => { e.preventDefault(); if (clarification.trim()) void respond({ text: clarification.trim() }) }}><input aria-label="补充本地生活查询信息" value={clarification} onChange={e => setClarification(e.target.value)} /><button type="submit" className="primary-button" disabled={busy || !clarification.trim()}>继续</button></form>}
    <div className="discover-workspace"><section className="discover-results"><div className="discover-results-heading"><h2>{searchInfo ? `${items.length} 个匹配商家` : '附近商家'}</h2>{searchInfo && <span>高德地图 · 参考点周边{Math.round(searchInfo.radius_m / 1000)}公里</span>}</div>
      {searchInfo && <p className="discover-provenance">{searchInfo.ranking_scope}{(filters.min_rating != null || filters.max_cost != null) && '；未知评分/人均不纳入对应筛选。'}</p>}
      {!items.length && <div className="discover-empty"><Icon size={32} /><strong>{searchInfo ? '没有满足当前条件的商家' : '暂无检索结果'}</strong></div>}
      <div className="venue-list">{items.map((venue, index) => <article className={`venue-row ${selected?.id === venue.id ? 'selected' : ''}`} key={venue.id}>
        <div className="venue-image">{venue.photos?.[0] ? <img src={venue.photos[0].url} alt={venue.name} loading="lazy" onError={e => { e.currentTarget.style.display = 'none' }} /> : <Icon size={26} />}</div>
        <div className="venue-copy"><h3>{venue.name}</h3><p><MapPin size={12} />{venue.address || '地址未提供'}</p><div className="venue-facts"><span><Star size={12} />{venue.rating == null ? '评分暂无' : venue.rating}</span><span>{venue.cost == null ? '人均暂无' : `参考 ¥${venue.cost}/人`}</span><span>{distanceText(venue.distance_m)}</span></div></div>
        <button className="icon-button venue-select" disabled={busy || waitingExpired} title={choosingVenue ? `选择${venue.name}` : `查看${venue.name}详情`} aria-label={choosingVenue ? `选择${venue.name}` : `查看${venue.name}详情`} onClick={() => choosingVenue ? void choose(index + 1) : void submit('local_detail', venueParameters('local_detail', venue, searchInfo?.center || filters.anchor))}>{choosingVenue ? <Check size={19} /> : <ArrowUpRight size={19} />}</button>
      </article>)}</div>
    </section><aside className="venue-detail"><h2>{selected?.name || '商家详情'}</h2>{selected ? <>
      <p className="venue-detail-address">{selected.address || '地址未提供'}</p>
      <dl><div><dt>高德评分</dt><dd>{selected.rating ?? '暂无'}</dd></div><div><dt>参考人均</dt><dd>{selected.cost == null ? '暂无' : `¥${selected.cost}`}</dd></div><div><dt>联系电话</dt><dd>{selected.phone || '未提供'}</dd></div><div><dt>营业时间</dt><dd>{selected.opening_hours || '未提供，需向商家确认'}</dd></div></dl>
      {!!selected.photos?.length && <div className="venue-photo-strip">{selected.photos.slice(0, 3).map(photo => <img key={photo.url} src={photo.url} alt={photo.title || selected.name} loading="lazy" />)}</div>}
      <p className="venue-availability">{selected.availability === 'reported_closed' ? '高德已标记暂停营业或停业，请勿按正常营业安排出行。' : '实时营业、余位与排期未知。'}未生成任何预订或订单。</p>
      <p className="venue-availability">路线参考起点：{searchInfo?.anchor || filters.anchor}</p>
      <div className="venue-route-controls"><select aria-label="出行方式" value={mode} onChange={e => setMode(e.target.value)}><option value="driving">驾车</option><option value="walking">步行</option><option value="bicycling">骑行</option></select><button className="primary-button" disabled={busy} onClick={() => void submit('local_route', venueParameters('local_route', selected, searchInfo?.center || filters.anchor, mode))}><Navigation size={15} />规划路线</button></div>
      {selected.map_url && <a className="venue-map-link" href={selected.map_url} target="_blank" rel="noreferrer">在高德查看商家<ArrowUpRight size={14} /></a>}
      {routeReceipt ? <RoutePreview key={routeReceipt.job_id} route={routeReceipt} /> : route && <div className="venue-route-result"><strong>{(route.distance_m / 1000).toFixed(1)} 公里 · 约 {Math.max(1, Math.round(route.duration_s / 60))} 分钟</strong><span>正在读取路线地图…</span></div>}
    </> : <div className="venue-detail-empty"><MapPin size={25} /><p>尚未选定商家</p></div>}</aside></div>
    {(task?.answer || reply) && <section className="discover-answer"><h2>本次结果</h2><p aria-live="polite">{task?.answer || reply}</p></section>}
  </div>
}
