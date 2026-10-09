import { useEffect } from 'react'
import { Link } from 'react-router-dom'
export function NotFound() {
  useEffect(() => { document.title = 'Page introuvable · Comptes familiaux' }, [])
  return <section className="not-found"><span className="not-found-code">404</span><p className="eyebrow">PAGE INTROUVABLE</p><h1>Ce chemin n’existe pas.</h1><p>La page a peut-être changé d’adresse ou le lien est incomplet.</p><Link className="button button--primary" to="/">Revenir à la vue d’ensemble</Link></section>
}
