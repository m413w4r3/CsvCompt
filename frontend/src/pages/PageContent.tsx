import { Link } from 'react-router-dom'
import { EmptyState, PageHeader } from '../components/ui'

export interface PageCopy {
  eyebrow: string
  title: string
  description: string
  emptyTitle: string
  emptyDescription: string
}

export function PageContent({ copy }: { copy: PageCopy }) {
  return <>
    <PageHeader eyebrow={copy.eyebrow} title={copy.title} description={copy.description} />
    <section className="period-context" aria-label="Période de référence">
      <span className="context-mark" aria-hidden="true">2026</span>
      <div><strong>Votre année, à votre rythme</strong><p>Les mois en cours seront signalés comme partiels. Aucune comparaison ne sera extrapolée.</p></div>
      <Link to="/transactions" className="text-link">Voir les opérations <span aria-hidden="true">→</span></Link>
    </section>
    <EmptyState title={copy.emptyTitle} description={copy.emptyDescription} />
  </>
}
