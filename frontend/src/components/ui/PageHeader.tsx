import { useEffect } from 'react'

export function PageHeader({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  useEffect(() => { document.title = `${title} · Comptes familiaux` }, [title])
  return <header className="page-header"><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p className="page-description">{description}</p></header>
}
