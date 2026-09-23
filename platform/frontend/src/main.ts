import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { i18n, currentLocale } from './i18n'
import './style.css'

// The <html lang> should match the active locale from the first paint.
document.documentElement.lang = currentLocale()

// A rolling deploy rotates the hashed chunks this page was built against, so a
// lazy route import can 404 against a new pod. Vite fires vite:preloadError
// rather than throwing; reload onto the new bundle instead of leaving the user
// on a dead page. Rate-limited so a chunk that is genuinely gone surfaces as an
// error instead of a reload loop.
const RELOAD_KEY = 'vite:preloadError:reloadedAt'
window.addEventListener('vite:preloadError', (e) => {
  const last = Number(sessionStorage.getItem(RELOAD_KEY) || 0)
  if (Date.now() - last < 10_000) return
  sessionStorage.setItem(RELOAD_KEY, String(Date.now()))
  e.preventDefault()
  window.location.reload()
})

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(i18n)
app.mount('#app')
