// Helpers for displaying benchmark modules.

import { SUPPORTED_LOCALES, i18n } from '@/i18n'

/**
 * Display label for a module: the `moduleNames.<module_name>` translation when
 * any locale defines one (normal zh-CN → en fallback applies), else the
 * backend-provided display_name, else the raw module name. Display-only —
 * API payloads and map keys always keep the raw module_name. To localize a
 * module's name, add a `moduleNames` entry to BOTH locale files (CI enforces
 * en/zh-CN key parity — mirror the backend display_name in en); nothing else.
 */
export function localizedModuleName(moduleName: string, displayName?: string | null): string {
  const key = `moduleNames.${moduleName}`
  const g = i18n.global
  // te() checks one locale at a time; without this guard a missing key would
  // fall through to the humanize-the-key handler instead of display_name.
  if (SUPPORTED_LOCALES.some((locale) => g.te(key, locale))) return g.t(key)
  return displayName || moduleName
}

/**
 * Disambiguate a list of module names so duplicates are distinguishable.
 *
 * A benchmark can legitimately include the same module twice (e.g. two
 * `functional_acceptance` modules with different params). Returns labels
 * aligned 1:1 with the input: names that occur more than once get a
 * ` #1`, ` #2`, … suffix in order of appearance; unique names are unchanged.
 */
export function disambiguateNames(names: string[]): string[] {
  const counts = new Map<string, number>()
  for (const n of names) counts.set(n, (counts.get(n) ?? 0) + 1)

  const seen = new Map<string, number>()
  return names.map((n) => {
    if ((counts.get(n) ?? 0) <= 1) return n
    const idx = (seen.get(n) ?? 0) + 1
    seen.set(n, idx)
    return `${n} #${idx}`
  })
}
