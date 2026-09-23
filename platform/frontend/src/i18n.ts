// App-wide i18n (vue-i18n v11, composition mode). Display-only by design:
// translations never touch API payloads, metric keys, module names, slugs, or
// formula identifiers — those stay exactly as the backend emits them.
// Backend-sourced prose (module descriptions, run error strings) is shown
// verbatim and stays English until the backend grows error codes.
// Module DISPLAY labels are the one localized exception: an optional
// `moduleNames.<module_name>` entry overrides the backend display_name
// (utils/modules.ts localizedModuleName) — labels only, never keys.
import { createI18n } from 'vue-i18n'
import en from './locales/en.json'
import zhCN from './locales/zh-CN.json'

export const SUPPORTED_LOCALES = ['zh-CN', 'en'] as const
export type AppLocale = (typeof SUPPORTED_LOCALES)[number]

const STORAGE_KEY = 'locale'

function initialLocale(): AppLocale {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'zh-CN' || saved === 'en') return saved
  // English unless the browser asks for Chinese; a saved choice wins over both.
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en'
}

// Last-resort fallback: vue-i18n renders the raw dotted key ("metricGroups.misc")
// when a message is missing from EVERY locale. Degrade to readable English
// derived from the key's last segment instead ("metricGroups.misc" -> "Misc",
// "common.status.queued" -> "Queued"). Verified semantics: the `missing`
// handler's return value is used only after the zh-CN -> en fallback chain
// fails, so real English translations always win for zh-only misses.
function humanizeKey(key: string): string {
  const last = key.split('.').pop() || key
  const words = last
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .toLowerCase()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export const i18n = createI18n({
  legacy: false,
  locale: initialLocale(),
  fallbackLocale: 'en',
  missingWarn: false,
  fallbackWarn: false,
  missing: (_locale, key) => {
    if (import.meta.env.DEV) console.warn(`[i18n] missing key: ${key}`)
    return humanizeKey(key)
  },
  messages: { 'zh-CN': zhCN, en },
})

export function setLocale(locale: AppLocale) {
  i18n.global.locale.value = locale
  localStorage.setItem(STORAGE_KEY, locale)
  document.documentElement.lang = locale
}

export function currentLocale(): AppLocale {
  return i18n.global.locale.value as AppLocale
}
