import { Link } from 'react-router-dom'
import { Card } from './Card'

export function EmptyState({ title, description, action = true }: { title: string; description: string; action?: boolean }) {
  return <Card className="empty-state">
    <div className="empty-mark" aria-hidden="true"><span>＋</span></div>
    <div><h2>{title}</h2><p>{description}</p>
      {action && <Link className="text-link" to="/transactions">Importer un relevé <span aria-hidden="true">↗</span></Link>}
    </div>
  </Card>
}
