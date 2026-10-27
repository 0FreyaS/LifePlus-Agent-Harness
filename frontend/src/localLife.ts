export type Category = 'restaurant' | 'ktv' | 'cinema' | 'cafe'
export type Venue = { id: string; name: string; address?: string | null; location: string; city?: string | null; district?: string | null; type?: string | null; distance_m?: number | null; rating?: number | null; cost?: number | null; phone?: string | null; opening_hours?: string | null; availability?: 'reported_closed' | 'unknown'; photos?: { url: string; title?: string | null }[]; map_url?: string | null }
export type LocalFilters = { category: Category; anchor: string; city: string; radius_m: number; keyword: string; sort_by: 'distance' | 'rating'; min_rating?: number; max_cost?: number }
export const categoryNames: Record<Category, string> = { restaurant: '饭店', ktv: 'KTV', cinema: '电影院', cafe: '咖啡店' }
export function venueParameters(action: 'local_detail' | 'local_route', venue: Venue, anchor: string, mode = 'driving'): Record<string, unknown> {
  return action === 'local_detail' ? { poi_id: venue.id } : { poi_id: venue.id, anchor, travel_mode: mode }
}
export function distanceText(value?: number | null) { return value == null ? '距离未知' : value < 1000 ? `${Math.round(value)} 米` : `${(value / 1000).toFixed(1)} 公里` }
