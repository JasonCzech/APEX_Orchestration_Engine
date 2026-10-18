import { Link } from 'react-router'

export function RerunGuidance({ reason }: { reason: string }) {
  return <p>{reason}{' '}<Link to="/cycles">Review test cycles</Link></p>
}
