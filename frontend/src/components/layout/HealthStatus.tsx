import { useQuery } from '@tanstack/react-query'
import { ApiError, getHealth } from '../../lib/api'
import { Badge } from '../ui/Badge'

export function HealthStatus() {
  const health = useQuery({ queryKey: ['health'], queryFn: ({ signal }) => getHealth(signal), refetchInterval: 60_000, retry: false })
  const state = health.isPending ? 'loading' : health.isSuccess ? (health.data.status === 'ok' && health.data.storage_status === 'ok' ? 'ok' : 'error') : health.error instanceof ApiError && health.error.status > 0 ? 'error' : 'offline'
  const text = state === 'loading' ? 'Vérification…' : state === 'ok' ? 'Serveur local connecté' : state === 'offline' ? 'Serveur local hors ligne' : 'Serveur local en erreur'
  return <Badge className={`health health--${state}`} title={health.isError ? health.error.message : undefined} aria-live="polite">
    <span className="health-dot" aria-hidden="true" />{text}
  </Badge>
}
