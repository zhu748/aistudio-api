import { onMounted, onUnmounted } from 'vue'

type Refresh = () => unknown | Promise<unknown>
const handlers = new Set<Refresh>()

export function usePageRefresh(refresh: Refresh) {
  onMounted(() => handlers.add(refresh))
  onUnmounted(() => handlers.delete(refresh))
}

export async function refreshCurrentPage() {
  const results = await Promise.allSettled([...handlers].map((refresh) => refresh()))
  return results.every((result) => result.status === 'fulfilled' && result.value !== false)
}
