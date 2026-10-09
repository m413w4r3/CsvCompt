import { Card } from '../../components/ui/Card'
import { ChartFrame } from '../../components/charts/ChartFrame'
import { PageContent } from '../PageContent'

export function Overview() {
  return <div className="overview-page">
    <PageContent copy={{
      eyebrow: 'TABLEAU DE BORD', title: 'Vue d’ensemble', description: 'Un aperçu clair de la trésorerie et de la vie familiale, fondé uniquement sur vos opérations confirmées.',
      emptyTitle: 'Votre espace commence ici', emptyDescription: 'Importez un relevé pour suivre les mouvements du compte, puis distinguer les dépenses de vie familiale des transferts et autres flux.',
    }} />
    <div className="overview-bottom">
      <ChartFrame title="Activité du compte" description="Évolution des opérations confirmées sur la période sélectionnée." />
      <Card className="note-card"><span className="note-kicker">À GARDER EN TÊTE</span><h2>Des chiffres toujours expliqués</h2><p>Chaque indicateur reliera sa période, son périmètre et les opérations qui le composent.</p></Card>
    </div>
  </div>
}
