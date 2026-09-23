// Copy text to the pasteboard, including in insecure contexts.
//
// navigator.clipboard exists only on HTTPS/localhost; the platform is
// typically served over plain HTTP in-cluster, where it is undefined and a
// bare writeText() call throws. Fall back to an off-screen textarea +
// execCommand('copy') — deprecated, but it still writes the pasteboard over
// HTTP everywhere that matters.
//
// Returns true when the copy actually succeeded, so callers can decide what
// to do on failure (e.g. leave the text selected for a manual copy).
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through to the legacy path
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  // Off-screen but focusable; position:fixed avoids scrolling the page.
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  ta.setSelectionRange(0, text.length) // iOS Safari needs the explicit range
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(ta)
  return ok
}
