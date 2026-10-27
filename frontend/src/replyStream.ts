type ReplyEvent = { type: string; task_id: string; stream_id?: string; child_task_id?: string; delta?: unknown; answer?: unknown }

export function acceptStreamSequence(cursors: Record<string, number>, taskId: string, sequence: number): boolean {
  if (!Number.isSafeInteger(sequence) || sequence <= (cursors[taskId] || 0)) return false
  cursors[taskId] = sequence
  return true
}

export function applyReplyEvent(current: Record<string, string>, event: ReplyEvent): Record<string, string> {
  const id = event.stream_id || event.child_task_id || event.task_id
  if (event.type === 'answer.started') return { ...current, [id]: '' }
  if (event.type === 'answer.delta' && typeof event.delta === 'string') return { ...current, [id]: (current[id] || '') + event.delta }
  if (['answer.replaced', 'answer.completed', 'subtask.answer.completed'].includes(event.type) && typeof event.answer === 'string') return { ...current, [id]: event.answer }
  return current
}
