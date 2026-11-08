/**
 * Put text on the clipboard; resolves to whether it got there.
 *
 * navigator.clipboard only exists in a secure context, and the UI is often
 * opened over plain http on a LAN address, so fall back to the older
 * execCommand('copy') on a hidden textarea there.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Denied or unavailable; try the fallback.
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  // Inside the full-screen element when there is one: nothing outside it can take a selection.
  const host = document.fullscreenElement ?? document.body
  host.appendChild(area)
  try {
    area.select()
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    area.remove()
  }
}
