import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, ArrowUpRight, Bike, CarFront, Check, ChevronDown, ChevronUp, Clock3, Expand, Footprints, LocateFixed, MapPin, Minus, Navigation, Plus, RefreshCw, Route as RouteIcon, TriangleAlert } from 'lucide-react'
import { api, type RouteReceipt, type TravelMode } from './api'
import { routeDistance, routeDuration, routeMode, routeModeOrder, safeNavigationUrl } from './routeDisplay'
import { waitForPlannedRoute } from './routePlanning'
import './RoutePreview.css'

const modeIcons = { driving: CarFront, walking: Footprints, bicycling: Bike }

export function RoutePreview({ route: initial, large = false, onRouteChange }: { route: RouteReceipt; large?: boolean; onRouteChange?: (route: RouteReceipt) => void }) {
  const [route, setRoute] = useState(initial)
  const [stepsOpen, setStepsOpen] = useState(large)
  const [zoom, setZoom] = useState(0)
  const [revision, setRevision] = useState(0)
  const [image, setImage] = useState('')
  const [imageIdentity, setImageIdentity] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [planningMode, setPlanningMode] = useState<TravelMode | null>(null)
  const [choosing, setChoosing] = useState<number | null>(null)
  const [planningError, setPlanningError] = useState('')
  const [planningTask, setPlanningTask] = useState('')
  const action = useRef<AbortController | null>(null)
  const source = useRef(`${initial.task_id}:${initial.job_id}`)
  const busy = planningMode !== null || choosing !== null
  const travelModes = routeModeOrder(initial.default_travel_mode || initial.travel_mode).map(id => ({ id,
    label: initial.mode_choices?.find(choice => choice.travel_mode === id)?.label || routeMode(id), Icon: modeIcons[id] }))
  const ModeIcon = route.travel_mode === 'walking' ? Footprints : route.travel_mode === 'bicycling' ? Bike : CarFront
  const navigationUrl = safeNavigationUrl(route.navigation_url)
  const baseZoom = large ? route.map_zoom_large : route.map_zoom
  const mapIdentity = `${route.task_id}:${route.job_id}:${route.option_id ?? 0}:${zoom}:${large}:${revision}`

  useEffect(() => {
    const identity = `${initial.task_id}:${initial.job_id}`
    if (source.current !== identity) {
      source.current = identity; action.current?.abort(); action.current = null
      setRoute(initial); setZoom(0); setPlanningMode(null); setChoosing(null); setPlanningError(''); setPlanningTask('')
    } else setRoute(current => current.job_id === initial.job_id ? { ...initial, ...current, is_stale: initial.is_stale } : current)
  }, [initial])
  useEffect(() => () => { action.current?.abort() }, [])
  useEffect(() => {
    const timer = window.setInterval(() => setRoute(current => current.expires_at && Date.now() >= new Date(current.expires_at).getTime() ? { ...current, is_stale: true } : current), 30000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    if (initial.steps) return
    let live = true
    void api.route(initial.task_id, initial.job_id, initial.option_id).then(value => { if (live) { setRoute(current => current.job_id === initial.job_id && current.option_id === initial.option_id ? value : current); setDetailError('') } })
      .catch(e => { if (live) setDetailError(e.message) })
    return () => { live = false }
  }, [initial.task_id, initial.job_id, initial.option_id])

  useEffect(() => {
    const controller = new AbortController()
    let objectUrl = ''
    setLoading(true); setError(''); setImage('')
    void api.routeImage(route.task_id, route.job_id, zoom, large, controller.signal, route.option_id).then(blob => {
      if (controller.signal.aborted) return
      objectUrl = URL.createObjectURL(blob); setImage(objectUrl); setImageIdentity(mapIdentity); setLoading(false)
    }).catch(e => { if (!controller.signal.aborted) { setError(e.message); setLoading(false) } })
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [route.task_id, route.job_id, route.option_id, zoom, large, revision])

  async function chooseOption(option: number) {
    if (action.current || option === route.option_id) return
    const controller = new AbortController(); action.current = controller
    setChoosing(option); setPlanningError('')
    try {
      const selected = await api.route(route.task_id, route.job_id, option)
      if (!controller.signal.aborted) { setRoute(selected); setZoom(0); setDetailError(''); onRouteChange?.(selected) }
    } catch (e) { if (!controller.signal.aborted) setPlanningError(e instanceof Error ? e.message : '无法切换路线，原路线仍然保留') }
    finally { if (action.current === controller) { action.current = null; setChoosing(null) } }
  }

  async function replan(mode: TravelMode, force = false) {
    if (action.current || (!force && mode === route.travel_mode && !route.is_stale)) return
    const controller = new AbortController(); action.current = controller
    setPlanningMode(mode); setPlanningError(''); setPlanningTask('')
    try {
      const task = await api.replanRoute(route.task_id, route.job_id, mode, crypto.randomUUID())
      if (controller.signal.aborted) return
      setPlanningTask(task.task_id)
      const planned = await waitForPlannedRoute(api, task.task_id, controller.signal)
      if (planned.travel_mode !== mode || planned.origin !== route.origin || planned.destination !== route.destination) throw new Error('路线回执与本次选择不一致，已保留原路线')
      if (!controller.signal.aborted) { setRoute(planned); setZoom(0); setDetailError(''); onRouteChange?.(planned) }
    } catch (e) { if (!controller.signal.aborted) setPlanningError(e instanceof Error ? e.message : '路线规划失败，原路线仍然保留') }
    finally { if (action.current === controller) { action.current = null; setPlanningMode(null) } }
  }

  const planningControls = <div className="route-planning-controls"><span className="route-mode-field">出行方式</span><div className="route-mode-tabs" role="group" aria-label="出行方式">{travelModes.map(({ id, label, Icon }) => <button key={id} className={route.travel_mode === id ? 'active' : ''} aria-label={`${label}${route.travel_mode === id ? '，当前选择' : ''}`} aria-pressed={route.travel_mode === id} disabled={busy} onClick={() => void replan(id)}><Icon size={15} /><span className="route-mode-label">{label}</span><span className="route-mode-check">{route.travel_mode === id && <Check size={13} />}</span></button>)}</div><button className="route-refresh-plan" title="重新规划当前出行方式" aria-label="重新规划当前出行方式" disabled={busy} onClick={() => void replan(route.travel_mode, true)}><RefreshCw size={15} /></button>
      {planningMode && <span className="route-planning-status" role="status"><span className="route-map-spinner" />正在规划{routeMode(planningMode)}路线</span>}
      {choosing !== null && <span className="route-planning-status" role="status"><span className="route-map-spinner" />正在切换路线方案</span>}
      {planningError && <div className="route-planning-error" role="alert"><TriangleAlert size={14} /><span>{planningError}</span>{planningTask && <Link to={`/runs/${planningTask}`}>执行链路</Link>}</div>}
    </div>
  const optionButtons = <div className="route-options" role="group" aria-label="可选路线方案">{route.options?.map(option => <button key={option.option_id} className={option.option_id === route.option_id ? 'active' : ''} aria-label={`${option.label}，${routeDuration(option.duration_s)}，${routeDistance(option.distance_m)}${option.option_id === route.option_id ? '，当前选择' : ''}`} aria-pressed={option.option_id === route.option_id} disabled={busy} onClick={() => void chooseOption(option.option_id)}><span>{option.label}{option.option_id === route.option_id && <Check size={13} />}</span><strong>{routeDuration(option.duration_s)}</strong><small>{routeDistance(option.distance_m)}{Number(option.tolls) > 0 ? ` · 收费约 ¥${Number(option.tolls).toFixed(0)}` : ''}</small>{option.strategy && <small>{option.strategy}</small>}</button>)}</div>

  return <article className={`route-preview ${large ? 'expanded' : ''}`} aria-label="高德路线地图">
    <header className="route-preview-heading"><div><span className="route-source"><RouteIcon size={14} />高德地图</span><strong>{routeMode(route.travel_mode)}路线</strong></div><span className={`route-receipt-state ${route.is_stale ? 'stale' : ''}`}>{route.is_stale ? <><Clock3 size={12} />历史路线</> : <><Check size={12} />规划完成</>}</span></header>
    {planningControls}
    <div className="route-preview-map" aria-busy={loading}>
      {image && imageIdentity === mapIdentity && <img src={image} alt={`高德地图：从${route.origin_name}到${route.destination_name}的${routeMode(route.travel_mode)}路线`} onError={() => { setError('地图图片未能显示，请重试'); setImage('') }} />}
      {loading && <div className="route-map-placeholder" role="status"><span className="route-map-spinner" />正在加载高德地图</div>}
      {error && <div className="route-map-placeholder route-map-error" role="alert"><TriangleAlert size={22} /><span>{error}</span><button className="secondary-button" onClick={() => setRevision(value => value + 1)}><RefreshCw size={14} />重新加载地图</button></div>}
      <div className="route-map-tools" role="group" aria-label="地图视图"><button title="放大地图" aria-label="放大地图" disabled={loading || zoom >= 4 || baseZoom + zoom >= 17} onClick={() => setZoom(value => value + 1)}><Plus size={17} /></button><button title="缩小地图" aria-label="缩小地图" disabled={loading || zoom <= -4 || baseZoom + zoom <= 1} onClick={() => setZoom(value => value - 1)}><Minus size={17} /></button><button title="查看完整路线" aria-label="查看完整路线" disabled={loading} onClick={() => { setZoom(0); if (zoom === 0) setRevision(value => value + 1) }}><LocateFixed size={16} /></button></div>
      {!large && <Link className="route-map-expand" title="展开路线地图" aria-label="展开路线地图" to={`/routes/${route.task_id}?job=${encodeURIComponent(route.job_id)}&option=${route.option_id ?? 0}`}><Expand size={15} /></Link>}
    </div>
    <div className="route-preview-info"><div className="route-preview-metrics"><span className="route-mode"><ModeIcon size={16} />{routeMode(route.travel_mode)}</span><strong>{routeDuration(route.duration_s)}</strong><span>{routeDistance(route.distance_m)}</span>{Number(route.tolls) > 0 && <span>收费约 ¥{Number(route.tolls).toFixed(0)}</span>}</div>
      <span className="route-options-field">路线方案</span>
      {route.options?.length > 1 ? optionButtons : <p className="route-option-notice">本次可用 1 条{routeMode(route.travel_mode)}路线</p>}
      <div className="route-endpoints"><div><i className="route-point origin">起</i><span><strong>{route.origin_name}</strong>{route.origin_name === '起点' && <small>{route.origin}</small>}</span></div><div><i className="route-point destination">终</i><span><strong>{route.destination_name}</strong>{route.destination_name === '目的地' && <small>{route.destination}</small>}</span></div></div>
      {route.is_stale && <p className="route-age-notice"><Clock3 size={12} />路线快照已过期，请重新规划后出行</p>}
      {!route.geometry_available && <p className="route-age-notice">此回执未记录道路轨迹，地图仅标注起终点</p>}
      <div className="route-preview-actions"><button className="route-steps-toggle" aria-expanded={stepsOpen} onClick={() => setStepsOpen(value => !value)}><RouteIcon size={14} />路线步骤{stepsOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}</button>{navigationUrl && <a href={navigationUrl} target="_blank" rel="noreferrer" className="route-navigation-action" title="高德会按当前出行方式重新算路，可能与所选方案不同"><Navigation size={14} />在高德中导航<ArrowUpRight size={13} /></a>}</div>
      {stepsOpen && <div className="route-directions">{detailError ? <p role="alert">{detailError}</p> : route.steps?.length ? <ol>{route.steps.map((instruction, index) => <li key={`${index}-${instruction}`}><span>{index + 1}</span><p>{instruction}</p></li>)}</ol> : <p>该路线回执未提供详细步骤</p>}{route.steps_truncated && <small>展示前 {route.steps?.length || 0} 步，共 {route.step_count} 步；完整指引以高德为准</small>}</div>}
      <footer className="route-preview-attribution"><MapPin size={11} />路线数据来自高德地图{route.issued_at && <time> · {new Date(route.issued_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</time>}</footer>
    </div>
  </article>
}

export default function RoutePage() {
  const { taskId = '' } = useParams()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const job = params.get('job') || undefined
  const option = params.has('option') ? Number(params.get('option')) : undefined
  const [route, setRoute] = useState<RouteReceipt | null>(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let live = true
    setError(''); setRoute(null)
    void api.route(taskId, job, option).then(value => { if (live) setRoute(value) }).catch(e => { if (live) setError(e.message) })
    return () => { live = false }
  }, [taskId, job, option, revision])
  return <section className="route-page"><header className="route-page-heading"><div><Link to={route ? `/chat/${route.session_id}` : '/'}><ArrowLeft size={15} />返回对话</Link><h1>出行路线</h1><p>{route?.query || '高德路线详情'}</p></div>{route && <Link className="secondary-button" to={`/runs/${route.task_id}`}><RouteIcon size={14} />执行链路</Link>}</header>
    {error && <div className="inline-error" role="alert">{error}<button className="secondary-button" onClick={() => setRevision(value => value + 1)}>重试</button></div>}
    {route ? <RoutePreview key={route.job_id} route={route} large onRouteChange={selected => navigate(`/routes/${selected.task_id}?job=${encodeURIComponent(selected.job_id)}&option=${selected.option_id}`, { replace: true })} /> : !error && <div className="route-page-loading" role="status">正在读取路线…</div>}
  </section>
}
