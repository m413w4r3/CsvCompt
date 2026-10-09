export function Skeleton({ lines = 3 }: { lines?: number }) {
  return <div className="skeleton-stack" aria-hidden="true">{Array.from({ length: lines }, (_, index) => <span className="skeleton-line" key={index} />)}</div>
}
