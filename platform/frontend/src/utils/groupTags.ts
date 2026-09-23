// Display grouping for the benchmarks list.
//
// A benchmark carries `group_tags: string[]`, each a top-down path of 1-3
// levels joined by "/": "Top", "Top/Mid" or "Top/Mid/Low". The backend keeps
// that flat (it only validates the string shape — see normalize_group_tags in
// app/schemas/benchmarks.py, and keep the constants here in sync); the tree
// is assembled here, on the client, purely for presentation. A benchmark
// listed under several paths appears under each of them.

import type { Benchmark } from '@/api/client'

export const GROUP_TAG_MAX_DEPTH = 3
export const GROUP_TAG_SEGMENT_MAX = 60
export const GROUP_TAG_MAX_COUNT = 20
export const GROUP_TAG_SEPARATOR = '/'

/** One row in the editor: a fixed-width tuple, top-down, "" = unused level. */
export type GroupTagLevels = [string, string, string]

export interface GroupNode {
  /** Level name as shown (trimmed). */
  name: string
  /** Full path, e.g. "Top/Mid" — stable key for collapse state. */
  path: string
  /** 1 = top, 2 = mid, 3 = lowest. */
  depth: number
  /** Benchmarks tagged with exactly this path (not with a deeper one). */
  benchmarks: Benchmark[]
  children: GroupNode[]
}

/** Split a stored path into editor levels; trailing levels are "". */
export function splitGroupTag(tag: string): GroupTagLevels {
  const parts = tag
    .split(GROUP_TAG_SEPARATOR)
    .map((s) => s.trim())
    .slice(0, GROUP_TAG_MAX_DEPTH)
  return [parts[0] ?? '', parts[1] ?? '', parts[2] ?? '']
}

/**
 * Join editor levels back into a stored path, or "" if the row is blank.
 * Levels are top-down: once a level is empty, everything below is dropped
 * (the editor also disables the lower inputs, so this is belt and braces).
 */
export function joinGroupTag(levels: GroupTagLevels): string {
  const kept: string[] = []
  for (const raw of levels) {
    const seg = raw.trim()
    if (!seg) break
    kept.push(seg)
  }
  return kept.join(GROUP_TAG_SEPARATOR)
}

/**
 * Build the grouping tree for a list of benchmarks.
 *
 * Groups at every level are sorted by name (locale-aware, case-insensitive);
 * benchmarks inside a node keep the order they arrived in. Untagged
 * benchmarks are returned separately so the caller can render them as a
 * "Not grouped" bucket with a localised label, always last.
 */
export function buildGroupTree(benchmarks: Benchmark[]): {
  roots: GroupNode[]
  ungrouped: Benchmark[]
} {
  const rootMap = new Map<string, GroupNode>()
  const ungrouped: Benchmark[] = []

  for (const b of benchmarks) {
    const tags = (b.group_tags ?? []).map((t) => joinGroupTag(splitGroupTag(t))).filter(Boolean)
    if (tags.length === 0) {
      ungrouped.push(b)
      continue
    }
    // A benchmark tagged twice with the same path (shouldn't happen — the
    // backend dedupes) must still render once per group.
    for (const tag of new Set(tags)) {
      const segments = tag.split(GROUP_TAG_SEPARATOR)
      let level = rootMap
      let node: GroupNode | undefined
      let path = ''
      segments.forEach((seg, i) => {
        path = path ? `${path}${GROUP_TAG_SEPARATOR}${seg}` : seg
        let next = level.get(seg)
        if (!next) {
          next = { name: seg, path, depth: i + 1, benchmarks: [], children: [] }
          level.set(seg, next)
          if (node) node.children.push(next)
        }
        node = next
        level = childMap(next)
      })
      node!.benchmarks.push(b)
    }
  }

  const roots = [...rootMap.values()]
  sortTree(roots)
  return { roots, ungrouped }
}

// Per-node child lookup, kept out of GroupNode so the public shape stays
// plain data (renderable, serialisable).
const childMaps = new WeakMap<GroupNode, Map<string, GroupNode>>()
function childMap(node: GroupNode): Map<string, GroupNode> {
  let m = childMaps.get(node)
  if (!m) {
    m = new Map()
    childMaps.set(node, m)
  }
  return m
}

function sortTree(nodes: GroupNode[]): void {
  nodes.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }))
  for (const n of nodes) sortTree(n.children)
}

/** Total benchmarks under a node, counting one per tag occurrence. */
export function countInTree(node: GroupNode): number {
  return node.benchmarks.length + node.children.reduce((sum, c) => sum + countInTree(c), 0)
}

/**
 * Every distinct path prefix in use across `benchmarks`, per level, for
 * editor autocomplete: level 1 suggestions are all top names; level 2/3
 * suggestions are scoped to the parent chosen in the row.
 */
export function groupTagSuggestions(benchmarks: Benchmark[]): Set<string> {
  const paths = new Set<string>()
  for (const b of benchmarks) {
    for (const tag of b.group_tags ?? []) {
      const levels = splitGroupTag(tag)
      let prefix = ''
      for (const seg of levels) {
        if (!seg) break
        prefix = prefix ? `${prefix}${GROUP_TAG_SEPARATOR}${seg}` : seg
        paths.add(prefix)
      }
    }
  }
  return paths
}

/** Children of a path prefix ("" = top level) from a suggestion set. */
export function suggestionsUnder(paths: Set<string>, parent: string): string[] {
  const out: string[] = []
  for (const p of paths) {
    const parts = p.split(GROUP_TAG_SEPARATOR)
    const head = parts.slice(0, -1).join(GROUP_TAG_SEPARATOR)
    if (head === parent) out.push(parts[parts.length - 1])
  }
  return out.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }))
}

// ---------------------------------------------------------------------------
// Path-level editing (admin "Groups" tab)
//
// The list views above are benchmark-centric: they ask "where does this
// benchmark appear?". The group manager is path-centric — it edits the tag
// *paths* themselves (rename a level, move a subtree, drop a group) and
// rewrites every benchmark's flat tag list as a consequence. There is no group
// entity server-side, so a rename here is "rewrite this prefix on every
// benchmark that carries it"; these helpers are that rewrite, kept pure so the
// component only deals with drafts.
// ---------------------------------------------------------------------------

/** Number of levels in a path ("A/B" -> 2). */
export function pathDepth(path: string): number {
  return path ? path.split(GROUP_TAG_SEPARATOR).length : 0
}

/** True when `tag` is `path` itself or sits under it. Compares whole segments,
 *  so "AB" is not under "A". Case-sensitive, like the rest of the feature. */
export function isUnderPath(tag: string, path: string): boolean {
  return tag === path || tag.startsWith(path + GROUP_TAG_SEPARATOR)
}

export type GroupPathError = 'empty' | 'emptySegment' | 'tooDeep' | 'segmentTooLong'

/**
 * Validate a path typed into the group manager, mirroring the backend's
 * normalize_group_tags. Returns null when it is usable, otherwise which rule
 * it broke (the caller maps that to a localised message).
 */
export function validateGroupPath(path: string): GroupPathError | null {
  const segments = path.split(GROUP_TAG_SEPARATOR).map((s) => s.trim())
  // A trailing separator is a typing artefact, as it is server-side.
  while (segments.length && segments[segments.length - 1] === '') segments.pop()
  if (!segments.length) return 'empty'
  if (segments.some((s) => !s)) return 'emptySegment'
  if (segments.length > GROUP_TAG_MAX_DEPTH) return 'tooDeep'
  if (segments.some((s) => s.length > GROUP_TAG_SEGMENT_MAX)) return 'segmentTooLong'
  return null
}

/** Canonical form of a valid path: segments trimmed, trailing blanks dropped. */
export function normalizeGroupPath(path: string): string {
  const segments = path.split(GROUP_TAG_SEPARATOR).map((s) => s.trim())
  while (segments.length && segments[segments.length - 1] === '') segments.pop()
  return segments.join(GROUP_TAG_SEPARATOR)
}

/** Deepest level reached below `path` in `paths`, relative to `path` itself
 *  (1 = the path exists but has no children). Used to refuse a move that would
 *  push a subtree past GROUP_TAG_MAX_DEPTH. */
export function relativeSubtreeDepth(paths: Iterable<string>, path: string): number {
  let deepest = 1
  const base = pathDepth(path)
  for (const p of paths) {
    if (isUnderPath(p, path)) deepest = Math.max(deepest, pathDepth(p) - base + 1)
  }
  return deepest
}

/** Rewrite `from` (and everything under it) to `to`, dropping duplicates that
 *  a merge into an existing path can create. Other tags keep their order. */
export function renamePathIn(tags: string[], from: string, to: string): string[] {
  const out: string[] = []
  for (const tag of tags) {
    const next = isUnderPath(tag, from) ? to + tag.slice(from.length) : tag
    if (!out.includes(next)) out.push(next)
  }
  return out
}

/** Drop `path` and everything under it. */
export function removePathIn(tags: string[], path: string): string[] {
  return tags.filter((t) => !isUnderPath(t, path))
}

/** A path-only tree: every node in `paths` plus the ancestors they imply, so
 *  a benchmark tagged only "A/B/C" still renders under an "A" heading. */
export interface GroupPathNode {
  name: string
  path: string
  depth: number
  children: GroupPathNode[]
}

export function buildGroupPathTree(paths: Iterable<string>): GroupPathNode[] {
  const roots: GroupPathNode[] = []
  const byPath = new Map<string, GroupPathNode>()
  for (const raw of paths) {
    const path = normalizeGroupPath(raw)
    if (!path) continue
    let prefix = ''
    let parent: GroupPathNode | undefined
    path.split(GROUP_TAG_SEPARATOR).forEach((seg, i) => {
      prefix = prefix ? `${prefix}${GROUP_TAG_SEPARATOR}${seg}` : seg
      let node = byPath.get(prefix)
      if (!node) {
        node = { name: seg, path: prefix, depth: i + 1, children: [] }
        byPath.set(prefix, node)
        ;(parent ? parent.children : roots).push(node)
      }
      parent = node
    })
  }
  sortPathTree(roots)
  return roots
}

function sortPathTree(nodes: GroupPathNode[]): void {
  nodes.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }))
  for (const n of nodes) sortPathTree(n.children)
}
