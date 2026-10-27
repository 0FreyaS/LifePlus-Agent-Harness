export function taskEventStatus(type: string): string | undefined {
  return ({ 'task.queued': 'queued', 'task.retry_requested': 'queued', 'task.refresh_requested': 'queued', 'task.started': 'running',
    'task.completed': 'completed', 'task.partial': 'partial', 'task.failed': 'failed',
    'task.cancelled': 'cancelled', 'task.interrupted': 'interrupted' } as Record<string, string>)[type]
}
