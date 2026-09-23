import { marked } from 'marked'
import DOMPurify from 'dompurify'

// Renders user-authored markdown (e.g. a submission's description detail) to
// sanitized HTML. Always passed through DOMPurify — the source is untrusted
// user input shown to OTHER users on public pages, so raw v-html would be an
// XSS hole.

// Open links in a new tab without leaking the opener. Registered once at
// module scope; a hook (vs. post-editing the string) keeps the attributes
// inside the sanitized output.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

export function renderMarkdown(source: string): string {
  const html = marked.parse(source, { async: false, gfm: true, breaks: true })
  return DOMPurify.sanitize(html)
}
