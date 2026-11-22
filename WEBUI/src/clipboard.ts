// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

import { t } from './i18n'

/**
 * Copy text to the clipboard. navigator.clipboard only exists on HTTPS/localhost, and
 * schools may open SMP over plain http, so fall back to the old execCommand trick and,
 * as a last resort, a prompt the user can copy from. Returns true if it was copied.
 */
export async function copyText(text: string, promptLabel = t('common.copy_prompt')): Promise<boolean> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* fall through */
  }
  if (legacyCopy(text)) return true
  window.prompt(promptLabel, text)
  return false
}

function legacyCopy(text: string): boolean {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  try {
    return document.execCommand('copy') // deprecated, but the only option without HTTPS
  } catch {
    return false
  } finally {
    ta.remove()
  }
}
