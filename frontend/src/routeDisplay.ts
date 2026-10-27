export function routeDistance(meters: number): string {
  if (!Number.isFinite(meters) || meters <= 0) return '距离未知'
  return meters < 1000 ? `${Math.round(meters)} 米` : `${(meters / 1000).toFixed(1)} 公里`
}

export function routeDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '用时未知'
  const minutes = Math.max(1, Math.ceil(seconds / 60))
  return minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时${minutes % 60 ? ` ${minutes % 60} 分钟` : ''}`
}

export function routeMode(mode: string): string {
  return ({ driving: '驾车', walking: '步行', bicycling: '骑行' } as Record<string, string>)[mode] || '出行'
}

export function routeModeOrder(preferred: string): ('driving' | 'walking' | 'bicycling')[] {
  const modes: ('driving' | 'walking' | 'bicycling')[] = ['driving', 'walking', 'bicycling']
  const selected = modes.find(mode => mode === preferred) || 'driving'
  return [selected, ...modes.filter(mode => mode !== selected)]
}

export function safeNavigationUrl(value: string): string | null {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && url.hostname === 'uri.amap.com' && url.pathname === '/navigation' ? url.href : null
  } catch { return null }
}

export function messageTaskId(id: unknown): string {
  return typeof id === 'string' ? id.replace(/:(?:user|assistant)$/, '') : ''
}

export function withoutStandaloneRouteLinks(content: string): string {
  return content.split('\n').filter(line => !safeNavigationUrl(line.trim())).join('\n').trim() || '路线已规划。'
}
