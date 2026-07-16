/** Largest offset accepted by the backend's database-backed list routes. */
export const MAX_LIST_OFFSET = 10_000

interface FetchAllOffsetPagesOptions<T> {
  label: string
  pageSize: number
  fetchPage: (limit: number, offset: number) => Promise<T[]>
  maxOffset?: number
}

interface FindInOffsetPagesOptions<T> extends FetchAllOffsetPagesOptions<T> {
  predicate: (item: T) => boolean
}

function assertPageSize(label: string, pageSize: number): void {
  if (!Number.isSafeInteger(pageSize) || pageSize < 1) {
    throw new Error(`${label} pagination is misconfigured.`)
  }
}

function nextPageOffset(
  label: string,
  currentOffset: number,
  pageLength: number,
  maxOffset: number,
): number {
  const nextOffset = currentOffset + pageLength
  if (nextOffset <= currentOffset || nextOffset > maxOffset) {
    throw new Error(`${label} could not be loaded completely within the pagination limit.`)
  }
  return nextOffset
}

/**
 * Collect an offset-paginated array endpoint without silently treating its
 * first page as the complete catalog. A hard offset ceiling keeps a broken or
 * changing endpoint from causing an unbounded request loop; reaching that
 * ceiling with another full page fails visibly because completeness cannot be
 * established.
 */
export async function fetchAllOffsetPages<T>({
  label,
  pageSize,
  fetchPage,
  maxOffset = MAX_LIST_OFFSET,
}: FetchAllOffsetPagesOptions<T>): Promise<T[]> {
  assertPageSize(label, pageSize)

  const items: T[] = []
  let offset = 0
  for (;;) {
    const page = await fetchPage(pageSize, offset)
    if (page.length > pageSize) {
      throw new Error(`${label} returned an invalid oversized page.`)
    }
    items.push(...page)
    if (page.length < pageSize) return items
    offset = nextPageOffset(label, offset, page.length, maxOffset)
  }
}

/**
 * Search an offset-paginated endpoint without fetching pages after the first
 * match, while retaining the same oversized-page and maximum-offset guards.
 */
export async function findInOffsetPages<T>({
  label,
  pageSize,
  fetchPage,
  predicate,
  maxOffset = MAX_LIST_OFFSET,
}: FindInOffsetPagesOptions<T>): Promise<T | undefined> {
  assertPageSize(label, pageSize)

  let offset = 0
  for (;;) {
    const page = await fetchPage(pageSize, offset)
    if (page.length > pageSize) {
      throw new Error(`${label} returned an invalid oversized page.`)
    }
    const match = page.find(predicate)
    if (match !== undefined) return match
    if (page.length < pageSize) return undefined
    offset = nextPageOffset(label, offset, page.length, maxOffset)
  }
}
