export interface PageSelection { offset: number; limit: number }

/** Bounded lists share controls while each API supplies its supported sizes. */
export function Pagination({ label, offset, limit, hasNext, pending, options = [10, 25, 50, 100], onChange }: PageSelection & {
  label: string; hasNext: boolean; pending: boolean; options?: readonly number[]
  onChange: (page: PageSelection) => void
}) {
  const sizes = [...new Set([...options, limit])].filter(value => Number.isInteger(value) && value > 0).sort((a, b) => a - b)
  return <nav className="ux-actions ux-pagination" aria-label={`${label} pages`}>
    <label>Rows <select aria-label={`${label} rows per page`} value={limit} disabled={pending}
      onChange={(event) => onChange({ offset: 0, limit: Number(event.target.value) })}>
      {sizes.map((value) => <option key={value} value={value}>{value}</option>)}
    </select></label>
    <button type="button" className="btn btn-secondary" disabled={pending || offset <= 0} onClick={() => onChange({ offset: Math.max(0, offset - limit), limit })}>Previous</button>
    <span>Page {Math.floor(offset / limit) + 1}</span>
    <button type="button" className="btn btn-secondary" disabled={pending || !hasNext} onClick={() => onChange({ offset: offset + limit, limit })}>Next</button>
  </nav>
}
