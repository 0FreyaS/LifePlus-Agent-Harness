import type { RouteReceipt, Task } from './api'

function assertActive(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('操作已取消', 'AbortError')
}

const timeoutMessage = '等待路线结果超时，请通过执行链路查看任务；原路线仍然保留'

function bounded<T>(promise: Promise<T>, signal: AbortSignal, deadline: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const cleanup = () => { clearTimeout(timer); signal.removeEventListener('abort', cancel) }
    const cancel = () => { cleanup(); reject(new DOMException('操作已取消', 'AbortError')) }
    const timer = setTimeout(() => { cleanup(); reject(new Error(timeoutMessage)) }, Math.max(0, deadline - Date.now()))
    signal.addEventListener('abort', cancel, { once: true })
    promise.then(value => { cleanup(); resolve(value) }, error => { cleanup(); reject(error) })
    if (signal.aborted) cancel()
  })
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const done = () => { signal.removeEventListener('abort', cancel); resolve() }
    const timer = setTimeout(done, ms)
    const cancel = () => { clearTimeout(timer); signal.removeEventListener('abort', cancel); reject(new DOMException('操作已取消', 'AbortError')) }
    signal.addEventListener('abort', cancel, { once: true })
    if (signal.aborted) cancel()
  })
}

export async function waitForPlannedRoute(client: { task: (id: string) => Promise<Task>; route: (id: string) => Promise<RouteReceipt> },
  taskId: string, signal: AbortSignal, budgetMs = 90000, intervalMs = 500): Promise<RouteReceipt> {
  const deadline = Date.now() + budgetMs
  while (Date.now() < deadline) {
    assertActive(signal)
    const task = await bounded(client.task(taskId), signal, deadline)
    assertActive(signal)
    if (task.status === 'completed') {
      const route = await bounded(client.route(taskId), signal, deadline)
      assertActive(signal)
      return route
    }
    if (['failed', 'partial', 'cancelled', 'interrupted'].includes(task.status)) throw new Error(task.answer || '这次路线规划没有完成，原路线仍然保留')
    if (task.status === 'waiting_user') throw new Error('路线任务需要补充信息，请返回对话继续处理')
    await pause(Math.min(intervalMs, Math.max(0, deadline - Date.now())), signal)
  }
  throw new Error(timeoutMessage)
}
