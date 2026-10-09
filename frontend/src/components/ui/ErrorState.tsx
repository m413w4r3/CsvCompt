import { Button } from './Button'

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div className="error-state" role="alert"><p>{message}</p><Button onClick={onRetry}>Réessayer</Button></div>
}
