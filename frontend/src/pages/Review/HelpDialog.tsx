import { ReviewDialog } from '../../components/classification/ReviewDialog'

export const SHORTCUTS: Array<{ keys: string[]; description: string }> = [
  { keys: ['j', 'k'], description: 'Groupe suivant / précédent' },
  { keys: ['Entrée'], description: 'Ouvrir la confirmation de la première proposition complète de l’opération active' },
  { keys: ['c'], description: 'Classer ou corriger l’opération active' },
  { keys: ['s'], description: 'Séparer l’opération active en plusieurs parts' },
  { keys: ['u'], description: 'Annuler la dernière décision de l’opération active' },
  { keys: ['?'], description: 'Afficher cette aide' },
  { keys: ['Échap'], description: 'Fermer la fenêtre et rendre le focus à son déclencheur' },
]

export function HelpDialog({ onClose }: { onClose: () => void }) {
  return (
    <ReviewDialog title="Raccourcis clavier" onClose={onClose}>
      <dl className="review-shortcuts">
        {SHORTCUTS.map((shortcut) => (
          <div key={shortcut.keys.join('+')}>
            <dt>{shortcut.keys.map((key, position) => <span key={key}>{position > 0 ? ' / ' : ''}<kbd>{key}</kbd></span>)}</dt>
            <dd>{shortcut.description}</dd>
          </div>
        ))}
      </dl>
      <p>
        Les raccourcis sont inactifs dans un champ de saisie, une liste déroulante et pendant qu’une fenêtre est ouverte. Aucun
        raccourci n’enregistre une décision sans afficher d’abord sa portée.
      </p>
      <div className="review-dialog-actions">
        <button className="button button--primary" type="button" onClick={onClose}>Fermer</button>
      </div>
    </ReviewDialog>
  )
}
