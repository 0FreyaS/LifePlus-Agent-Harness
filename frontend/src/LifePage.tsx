import { useEffect, useState } from 'react'
import { Bell, Check, ListTodo, RefreshCw, X } from 'lucide-react'
import { api, type LifeState, type ReminderNotification } from './api'
import './LifePage.css'

const timeText = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
const labels: Record<string, string> = { scheduled: '待提醒', delivered: '已通知', cancelled: '已取消' }

export default function LifePage() {
  const [state, setState] = useState<LifeState>({ itineraries: [], reminders: [] })
  const [notifications, setNotifications] = useState<ReminderNotification[]>([])
  const [error, setError] = useState('')

  async function refresh() {
    try {
      const [next, inbox] = await Promise.all([api.lifeState(), api.notifications()])
      setState(next); setNotifications(inbox); setError('')
    } catch (e) { setError(e instanceof Error ? e.message : '加载失败') }
  }

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 2000)
    return () => window.clearInterval(timer)
  }, [])

  async function cancel(id: string) {
    try { await api.cancelReminder(id); await refresh() }
    catch (e) { setError(e instanceof Error ? e.message : '取消失败') }
  }

  async function markRead(id: string) {
    try { await api.readNotification(id); await refresh() }
    catch (e) { setError(e instanceof Error ? e.message : '更新失败') }
  }

  return <section className="life-page">
    <div className="life-heading"><h1>行程与提醒</h1><button className="icon-button" title="刷新" onClick={() => void refresh()}><RefreshCw size={17} /></button></div>
    {error && <p role="alert" className="inline-error">{error}</p>}
    <section className="life-section"><h2><Bell size={18} />到期通知</h2>
      {notifications.length === 0 && <p className="life-empty">暂无到期通知</p>}
      {notifications.map(item => <article className={`life-row ${item.read_at ? '' : 'unread'}`} key={item.id}>
        <div><strong>{item.title}</strong><small>{timeText(item.created_at)}</small></div>
        {item.read_at ? <span className="life-status">已读</span> : <button className="icon-button" title="标为已读" onClick={() => void markRead(item.id)}><Check size={17} /></button>}
      </article>)}
    </section>
    <section className="life-section"><h2><Bell size={18} />提醒</h2>
      {state.reminders.length === 0 && <p className="life-empty">暂无提醒</p>}
      {state.reminders.map(item => <article className="life-row" key={item.id}>
        <div><strong>{item.title}</strong><small>{timeText(item.remind_at)}</small><small className="resource-id">{item.id}</small></div>
        <span className="life-status">{labels[item.status] || item.status}</span>
        {item.status === 'scheduled' && <button className="icon-button" title="取消提醒" onClick={() => void cancel(item.id)}><X size={17} /></button>}
      </article>)}
    </section>
    <section className="life-section"><h2><ListTodo size={18} />行程清单</h2>
      {state.itineraries.length === 0 && <p className="life-empty">暂无行程</p>}
      {state.itineraries.map(item => <article className="life-row" key={item.id}><div><strong>{item.title}</strong><small>{timeText(item.created_at)}</small></div></article>)}
    </section>
  </section>
}
