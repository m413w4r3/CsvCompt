export function Spinner({ label = 'Chargement en cours' }: { label?: string }) {
  return <span className="spinner" role="status"><span className="visually-hidden">{label}</span></span>
}
