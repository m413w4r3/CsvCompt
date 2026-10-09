import { useQuery } from '@tanstack/react-query'
import { ApiError, getHealth } from '../../lib/api'
import { Badge } from '../ui/Badge'

export function HealthStatus() {
  const health = useQuery({ queryKey: ['health'], queryFn: ({ signal }) => getHealth(signal), refetchInterval: 60_000, retry: false })
  let state: 'loading' | 'ok' | 'warning' | 'error' | 'offline' = 'loading'
  let text = 'Vérification…'
  let title: string | undefined

  if (health.isSuccess) {
    const { status, storage_status: storageStatus } = health.data
    if (storageStatus === 'degraded') {
      state = 'warning'
      text = 'Base locale à initialiser ou à migrer'
      title = 'La base locale nécessite une initialisation ou une migration avant utilisation complète.'
    } else if (storageStatus === 'unavailable') {
      state = 'error'
      text = 'Base locale indisponible'
      title = 'La base locale ne peut pas être consultée. Le reste de l’interface demeure disponible.'
    } else if (status === 'ok') {
      state = 'ok'
      text = 'Serveur local connecté'
    } else if (status === 'unavailable') {
      state = 'error'
      text = 'Serveur local indisponible'
      title = 'Le serveur local ne peut pas traiter les requêtes pour le moment.'
    } else {
      state = 'warning'
      text = 'Serveur local en mode dégradé'
      title = 'Le serveur local répond, mais certaines capacités sont réduites.'
    }
  } else if (health.isError) {
    if (health.error instanceof ApiError && health.error.status > 0) {
      state = 'error'
      text = 'Serveur local en erreur'
      title = health.error.message
    } else {
      state = 'offline'
      text = 'Serveur local hors ligne'
    }
  }

  return <Badge className={`health health--${state}`} title={title} aria-live="polite">
    <span className="health-dot" aria-hidden="true" />{text}
  </Badge>
}
