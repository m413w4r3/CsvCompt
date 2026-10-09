import { useId } from 'react'
import { Card } from '../ui/Card'

export function ChartFrame({ title, description }: { title: string; description: string }) {
  const id = useId()
  return <Card className="chart-frame" aria-labelledby={`${id}-title`}>
    <div><h2 id={`${id}-title`}>{title}</h2><p id={`${id}-description`}>{description}</p></div>
    <p className="chart-empty" role="status" aria-describedby={`${id}-description`}>Le graphique apparaîtra quand des opérations auront été importées.</p>
  </Card>
}
