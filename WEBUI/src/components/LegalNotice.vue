<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// The legal notice the license asks for, shown on the sign-in page and in the side bar.
//
//   ORIGINAL  the attribution every modified version must keep (NOTICE, AGPL 7(b)).
//             A fork shows it as "Based on SMP – School Management Portal".
//   SOURCE    where users get the source code of *this* version (AGPL section 13).
//             A fork that changes the code points this at its own repository.
import { t } from '../i18n'

const ORIGINAL = 'https://github.com/ticksparancsnok/SMP'
const SOURCE = ORIGINAL
const LICENSE = 'https://www.gnu.org/licenses/agpl-3.0.html'
</script>

<template>
  <p class="legal">
    <a :href="ORIGINAL" target="_blank" rel="noopener">SMP – School Management Portal</a>
    · <a :href="LICENSE" target="_blank" rel="noopener" :title="t('legal.license_title')">AGPL-3.0</a>
    · <a :href="SOURCE" target="_blank" rel="noopener">{{ t('legal.source') }}</a>
  </p>
</template>

<style scoped>
.legal {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--muted);
}
.legal a {
  color: inherit;
  text-decoration: underline;
  text-decoration-color: var(--border);
}
.legal a:hover {
  color: var(--text);
}
</style>
