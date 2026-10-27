import { fetchEventSource } from '@microsoft/fetch-event-source'

export type Session = { id: string; title: string; created_at: string; updated_at: string }
export type Review = { is_bad_case: boolean; category: string; root_cause: string; expected_answer: string; note: string; updated_at?: string }
export type Task = { id: string; session_id: string; user_id?: string; query: string; status: string; mode: string; answer?: string; domain?: string; review?: Review | null; interrupt_json?: string; interrupt?: Interrupt; last_seq: number; created_at: string; children?: Task[]; parent_task_id?: string; plan_json?: string; can_retry?: boolean }
export type Interrupt = { id: string; kind: 'clarification' | 'selection' | 'approval'; question: string; domain?: string; field?: string; arguments?: Record<string, unknown>; candidates?: Record<string, unknown>[]; operation_key?: string; issued_at?: string; expires_at?: string; expired?: boolean }
export type RunEvent = { seq: number; event_id: string; task_id: string; type: string; created_at: string; [key: string]: any }
export type Health = { status: string; default_task_mode: string; model_configured: boolean; model_name?: string | null; maps_configured: boolean; checkpoint_backend?: string; session_cache?: string; long_term_memory?: string; rag_backend?: string }
export type LifeState = { itineraries: { id: string; title: string; created_at: string }[]; reminders: { id: string; title: string; remind_at: string; status: string }[] }
export type ReminderNotification = { id: string; reminder_id: string; title: string; created_at: string; read_at?: string | null }
export type TravelMode = 'driving' | 'walking' | 'bicycling'
export type RouteOption = { option_id: number; label: string; strategy: string; distance_m: number; duration_s: number; tolls: string; geometry_available: boolean }
export type RouteReceipt = { task_id: string; root_task_id: string; session_id: string; job_id: string; query: string; origin: string; destination: string; origin_name: string; destination_name: string; travel_mode: TravelMode; distance_m: number; duration_s: number; tolls: string; source: string; issued_at?: string; expires_at?: string; is_stale: boolean; geometry_available: boolean; map_zoom: number; map_zoom_large: number; steps?: string[]; step_count: number; steps_truncated: boolean; navigation_url: string; option_id: number; options: RouteOption[]; alternatives_truncated?: boolean; default_travel_mode?: TravelMode; mode_choices?: {travel_mode: TravelMode; label: string; is_default: boolean}[] }
const headers = { 'Content-Type': 'application/json', 'X-Demo-User': localStorage.getItem('lifeplus-user') || 'demo-user' }
const apiBase = import.meta.env.VITE_API_BASE_URL || ''

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${apiBase}${url}`, { ...init, headers: { ...headers, ...(init.headers || {}) } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error((typeof body.detail === 'string' ? body.detail : body.detail?.message) || `请求失败 (${response.status})`)
  }
  return response.json()
}

export const api = {
  health: () => request<Health>('/api/health'),
  sessions: () => request<Session[]>('/api/sessions'),
  createSession: () => request<Session>('/api/sessions', { method: 'POST' }),
  messages: (id: string) => request<any[]>(`/api/sessions/${id}/messages`),
  sessionRoutes: (id: string) => request<RouteReceipt[]>(`/api/sessions/${id}/routes`),
  route: (id: string, job?: string, option?: number) => {
    const params = new URLSearchParams()
    if (job) params.set('job_id', job)
    if (option !== undefined) params.set('option', String(option))
    return request<RouteReceipt>(`/api/tasks/${id}/route?${params}`)
  },
  replanRoute: (id: string, job: string, mode: TravelMode, requestId: string) => request<{ task_id: string; status: string }>(`/api/tasks/${id}/route/replan`, { method: 'POST', body: JSON.stringify({ job_id: job, travel_mode: mode, request_id: requestId }) }),
  async routeImage(id: string, job: string, zoom: number, large: boolean, signal: AbortSignal, option?: number): Promise<Blob> {
    const params = new URLSearchParams({ job_id: job, zoom: String(zoom), large: String(large) })
    if (option !== undefined) params.set('option', String(option))
    const response = await fetch(`${apiBase}/api/tasks/${id}/route/map?${params}`, { headers, signal })
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(typeof body.detail === 'string' ? body.detail : '地图暂不可用')
    }
    return response.blob()
  },
  tasks: (sessionId?: string) => request<Task[]>(`/api/tasks${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`),
  runData: (filters: Record<string, string | number | undefined>) => {
    const params = new URLSearchParams()
    Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== '') params.set(key, String(value)) })
    return request<Task[]>(`/api/tasks?${params.toString()}`)
  },
  analytics: () => request<{ total: number; failed: number; reviewed: number; unreviewed: number; bad_cases: number }>('/api/analytics/summary'),
  review: (id: string) => request<Review | null>(`/api/tasks/${id}/review`),
  saveReview: (id: string, review: Omit<Review, 'updated_at'>) => request<Review>(`/api/tasks/${id}/review`, { method: 'PUT', body: JSON.stringify(review) }),
  task: (id: string) => request<Task>(`/api/tasks/${id}`),
  submit: (session: string, message: string, mode: string) => request<{ task_id: string; status: string }>(`/api/sessions/${session}/messages`, { method: 'POST', body: JSON.stringify({ message, mode, request_id: crypto.randomUUID() }) }),
  localTask: (session: string, action: 'local_search' | 'local_detail' | 'local_route', parameters: Record<string, unknown>) => request<{ task_id: string; status: string }>('/api/local-life/tasks', { method: 'POST', body: JSON.stringify({ session_id: session, action, parameters, mode: 'live', request_id: crypto.randomUUID() }) }),
  resume: (task: string, interrupt: Interrupt, action: string, value: Record<string, unknown> = {}) => request<{ task_id: string; created_task_id?: string }>(`/api/tasks/${task}/resume`, { method: 'POST', body: JSON.stringify({ interrupt_id: interrupt.id, action, value }) }),
  cancel: (task: string) => request(`/api/tasks/${task}/cancel`, { method: 'POST' }),
  retry: (task: string) => request<{ task_id: string; root_task_id: string; retried_task_ids: string[] }>(`/api/tasks/${task}/retry`, { method: 'POST' }),
  refreshTask: (task: string, interrupt: Interrupt) => request<{ task_id: string }>(`/api/tasks/${task}/refresh`, { method: 'POST', body: JSON.stringify({ interrupt_id: interrupt.id }) }),
  trace: (id: string) => request<RunEvent[]>(`/api/tasks/${id}/trace`),
  graph: (id: string) => request<{ nodes: { id: string; label: string; group: string }[]; edges: { source: string; target: string; label?: string }[] }>(`/api/tasks/${id}/graph`),
  diagnostics: (id: string) => request<{ wall_ms: number; work: { model_ms: number; model_attempts: number; tool_dispatch_ms: number; tool_timing_known_attempts: number; ready_queue_ms: number }; aggregation: string }>(`/api/tasks/${id}/diagnostics`),
  checkpoints: (id: string) => request<any[]>(`/api/sessions/${id}/checkpoints`),
  replay: (session: string, checkpoint: string) => request<{ task_id: string; session_id: string }>(`/api/sessions/${session}/checkpoints/${checkpoint}/replay`, { method: 'POST' }),
  memories: (id: string) => request<any[]>(`/api/sessions/${id}/memories`),
  putMemory: (session: string, key: string, value: string) => request(`/api/sessions/${session}/memories/${encodeURIComponent(key)}`, { method: 'PUT', body: JSON.stringify({ value }) }),
  deleteMemory: (session: string, key: string) => request(`/api/sessions/${session}/memories/${encodeURIComponent(key)}`, { method: 'DELETE' }),
  lifeState: () => request<LifeState>('/api/life/state'),
  notifications: () => request<ReminderNotification[]>('/api/notifications'),
  readNotification: (id: string) => request(`/api/notifications/${id}/read`, { method: 'POST' }),
  cancelReminder: (id: string) => request(`/api/reminders/${id}/cancel`, { method: 'POST' }),
  subscribe(task: string, after: number, onEvent: (event: RunEvent) => void, onError: (error: Error) => void) {
    const controller = new AbortController()
    fetchEventSource(`${apiBase}/api/tasks/${task}/events?after_seq=${after}`, {
      headers, signal: controller.signal, openWhenHidden: true,
      async onopen(response) { if (!response.ok) throw new Error(`事件流连接失败 (${response.status})`) },
      onmessage(message) {
        if (message.data) {
          try { onEvent(JSON.parse(message.data)) } catch { /* Ignore malformed server-sent events. */ }
        }
      },
      onerror(error) { onError(error); return 1000 },
    }).catch(() => undefined)
    return () => controller.abort()
  },
}

export function messageText(content: any): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) return content.map(item => typeof item === 'string' ? item : item?.text || '').join('')
  return String(content ?? '')
}
