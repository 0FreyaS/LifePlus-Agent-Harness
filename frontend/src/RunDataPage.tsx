import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Activity, ArrowRight, CircleAlert, CircleCheck, Clock3, Search, ShieldAlert } from 'lucide-react'
import { api, type Task } from './api'

const pageSize = 40

export default function RunDataPage() {
  const [rows, setRows] = useState<Task[]>([])
  const [summary, setSummary] = useState({ total: 0, failed: 0, reviewed: 0, unreviewed: 0, bad_cases: 0 })
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [domain, setDomain] = useState('')
  const [status, setStatus] = useState('')
  const [reviewState, setReviewState] = useState('')
  const [offset, setOffset] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [items, stats] = await Promise.all([
        api.runData({ q: submittedQuery, domain, status, review_state: reviewState, limit: pageSize, offset }),
        api.analytics(),
      ])
      setRows(items); setSummary(stats); setError('')
    } catch (e) { setError(e instanceof Error ? e.message : '读取执行数据失败') }
    finally { setLoading(false) }
  }, [submittedQuery, domain, status, reviewState, offset])

  useEffect(() => { void refresh() }, [refresh])

  function resetPage(update: () => void) { update(); setOffset(0) }

  return <main className="run-data-page">
    <header className="run-data-header"><div><span className="section-kicker">OBSERVABILITY · BAD CASE LOOP</span><h1>执行数据</h1><p>从用户原始输入到 Agent 回复与工具轨迹，检索问题、记录归因并沉淀改进样本。</p></div><button className="secondary-button" onClick={() => void refresh()}><Activity size={15} />刷新数据</button></header>
    <section className="data-stats">
      <Stat icon={<Activity size={16} />} label="任务总数" value={summary.total} />
      <Stat icon={<ShieldAlert size={16} />} label="Bad Case" value={summary.bad_cases} tone="bad" />
      <Stat icon={<Clock3 size={16} />} label="待归因" value={summary.unreviewed} tone="wait" />
      <Stat icon={<CircleAlert size={16} />} label="执行失败" value={summary.failed} tone="bad" />
    </section>
    <form className="data-filters" onSubmit={e => { e.preventDefault(); resetPage(() => setSubmittedQuery(query.trim())) }}>
      <label className="data-search"><Search size={15} /><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索用户输入或 Agent 回复" /><button type="submit">搜索</button></label>
      <select aria-label="按领域筛选" value={domain} onChange={e => resetPage(() => setDomain(e.target.value))}><option value="">全部领域</option><option value="navigation">地点与路线</option><option value="weather">天气</option><option value="music">音乐</option><option value="wellbeing">健康知识</option><option value="planning">行程提醒</option><option value="local_life">本地生活</option><option value="chat">通用对话</option></select>
      <select aria-label="按状态筛选" value={status} onChange={e => resetPage(() => setStatus(e.target.value))}><option value="">全部状态</option><option value="completed">已完成</option><option value="failed">失败</option><option value="waiting_user">等待用户</option><option value="cancelled">已取消</option><option value="interrupted">已中断</option></select>
      <select aria-label="按归因状态筛选" value={reviewState} onChange={e => resetPage(() => setReviewState(e.target.value))}><option value="">全部归因</option><option value="unreviewed">待归因</option><option value="bad_case">Bad Case</option><option value="reviewed">已审核</option></select>
    </form>
    {error && <div className="inline-error"><CircleAlert size={15} />{error}<button className="secondary-button small" onClick={() => void refresh()}>重试</button></div>}
    <section className="run-table-wrap"><table className="run-table"><thead><tr><th>用户输入 / Agent 回复</th><th>领域</th><th>状态</th><th>归因</th><th>时间</th><th /></tr></thead><tbody>
      {rows.map(task => <tr key={task.id}>
        <td className="run-content"><Link to={`/runs/${task.id}`}><strong>{task.query}</strong><span>{task.answer || (task.status === 'failed' ? '任务失败，打开轨迹查看具体错误' : '尚无最终回复')}</span></Link></td>
        <td><span className="domain-tag">{domainName(task.domain || 'chat')}</span></td>
        <td><span className={`status-badge ${task.status}`}><i />{statusName(task.status)}</span></td>
        <td>{task.review?.is_bad_case ? <span className="review-tag bad">Bad Case{task.review.category ? ` · ${categoryName(task.review.category)}` : ''}</span> : task.review ? <span className="review-tag reviewed"><CircleCheck size={12} />已审核</span> : <span className="review-tag pending">待归因</span>}</td>
        <td className="run-time">{new Date(task.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</td>
        <td><Link className="row-open" to={`/runs/${task.id}`} aria-label="打开执行详情"><ArrowRight size={15} /></Link></td>
      </tr>)}
      {!loading && rows.length === 0 && <tr><td className="table-empty" colSpan={6}><Search size={20} /><strong>没有匹配的执行记录</strong><span>试试清除筛选条件，或先发起一条对话任务。</span></td></tr>}
      {loading && <tr><td className="table-empty" colSpan={6}>正在读取执行数据…</td></tr>}
    </tbody></table></section>
    <footer className="data-pagination"><span>显示 {rows.length ? offset + 1 : 0}–{offset + rows.length} 条 · 共 {summary.total} 条执行记录</span><div><button className="secondary-button" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - pageSize))}>上一页</button><button className="secondary-button" disabled={rows.length < pageSize || loading} onClick={() => setOffset(offset + pageSize)}>下一页</button></div></footer>
  </main>
}

function Stat({ icon, label, value, tone = '' }: { icon: React.ReactNode; label: string; value: number; tone?: string }) {
  return <div className={`data-stat ${tone}`}><span className="data-stat-icon">{icon}</span><span><small>{label}</small><strong>{value.toLocaleString()}</strong></span></div>
}
function domainName(value: string) { return ({ navigation: '地点与路线', weather: '天气', music: '音乐', wellbeing: '健康知识', planning: '行程提醒', local_life: '本地生活', chat: '通用对话' } as Record<string, string>)[value] || value }
function statusName(value: string) { return ({ queued: '排队中', running: '执行中', waiting_user: '等待用户', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' } as Record<string, string>)[value] || value }
function categoryName(value: string) { return ({ intent: '意图', slot: '槽位', memory: '记忆', skill: 'Skill', tool: '工具', knowledge: '知识', policy: '策略', response_quality: '回复质量', latency: '时延', other: '其他' } as Record<string, string>)[value] || value }
