import { useEffect, useId, useRef, type ReactNode, type RefObject } from 'react'

const FOCUSABLE =
  'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'

interface Props {
  title: string
  onClose: () => void
  children: ReactNode
  /** Ferme avec Échap ; désactivé pendant une écriture pour ne pas perdre le résultat. */
  closeDisabled?: boolean
  initialFocus?: RefObject<HTMLElement | null>
  className?: string
}

/**
 * Dialogue modal accessible : focus initial, piège de tabulation, Échap, restitution du focus
 * au déclencheur. Le clic sur le fond ne ferme pas (une saisie en cours ne se perd pas).
 */
export function ReviewDialog({ title, onClose, children, closeDisabled = false, initialFocus, className = '' }: Props) {
  const titleId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  const disabledRef = useRef(closeDisabled)
  closeRef.current = onClose
  disabledRef.current = closeDisabled

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const dialog = dialogRef.current
    const target = initialFocus?.current ?? dialog?.querySelector<HTMLElement>(FOCUSABLE) ?? dialog
    target?.focus()

    function onKeyDown(event: KeyboardEvent) {
      if (!dialog) return
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        if (!disabledRef.current) closeRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)]
      if (items.length === 0) {
        event.preventDefault()
        dialog.focus()
        return
      }
      const first = items[0]!
      const last = items[items.length - 1]!
      const active = document.activeElement
      if (event.shiftKey && (active === first || active === dialog)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      } else if (active instanceof Node && !dialog.contains(active)) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      if (opener && document.contains(opener)) opener.focus()
    }
    // Le focus initial n'est donné qu'à l'ouverture.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="review-dialog-backdrop">
      <div
        ref={dialogRef}
        className={`review-dialog ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="review-dialog-heading">
          <h2 id={titleId}>{title}</h2>
          <button className="button" type="button" onClick={onClose} disabled={closeDisabled} aria-label="Fermer la fenêtre">
            Fermer
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
