// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

import { createRouter, createWebHistory } from 'vue-router'
import { canAnywhere, isStaff, loadSession, PLANNED, session } from './session'

// Each route may declare meta.perm: the nav hides it and the guard redirects home
// when the user lacks it (the API enforces permissions regardless).
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('./views/LoginView.vue'), meta: { public: true } },
    // Logged in, but without the app shell: shown until an admin-set password is replaced.
    { path: '/change-password', component: () => import('./views/ChangePasswordView.vue'), meta: { bare: true } },
    { path: '/', component: () => import('./views/DashboardView.vue') },
    { path: '/account', component: () => import('./views/AccountView.vue') },
    { path: '/users', component: () => import('./views/UsersView.vue'), meta: { perm: 'core.users.read' } },
    { path: '/groups', component: () => import('./views/GroupsView.vue'), meta: { perm: 'core.groups.read' } },
    { path: '/buildings', component: () => import('./views/BuildingsView.vue'), meta: { perm: 'core.buildings.read' } },
    { path: '/rooms', component: () => import('./views/RoomsView.vue'), meta: { perm: 'core.rooms.read' } },
    { path: '/devices', component: () => import('./views/DevicesView.vue'), meta: { perm: 'core.devices.read' } },
    { path: '/policies', component: () => import('./views/PoliciesView.vue'), meta: { perm: 'core.policies.read' } },
    { path: '/audit', component: () => import('./views/AuditView.vue'), meta: { perm: 'core.audit.read' } },
    { path: '/settings', component: () => import('./views/SettingsView.vue'), meta: { perm: 'core.settings.read' } },
    {
      path: '/shortlinks',
      component: () => import('./views/ShortlinksView.vue'),
      meta: { perm: 'shortlinks.links.create' },
    },
    {
      path: '/classroom',
      component: () => import('./views/ClassroomView.vue'),
      meta: { perm: 'classroom.screens.view' },
    },
    {
      path: '/software',
      component: () => import('./views/SoftwareView.vue'),
      meta: { perm: 'software.status.read' },
    },
    {
      path: '/dns',
      component: () => import('./views/DnsView.vue'),
      meta: { perm: 'dns.blocklist.read' },
    },
    // Planned services, modules without a UI yet, and unknown paths.
    { path: '/:pathMatch(.*)*', component: () => import('./views/PlaceholderView.vue') },
  ],
})

router.beforeEach(async (to) => {
  if (!session.loaded) await loadSession()
  if (to.meta.public) return session.me && to.path === '/login' ? '/' : true
  if (!session.me) return { path: '/login', query: to.fullPath !== '/' ? { next: to.fullPath } : {} }
  if (session.me.must_change_password) {
    if (to.path === '/change-password') return true
    return { path: '/change-password', query: to.fullPath !== '/' ? { next: to.fullPath } : {} }
  }
  if (to.path === '/change-password') return '/'
  const perm = to.meta.perm as string | undefined
  if (perm && !canAnywhere(perm)) return '/'
  if (!isStaff() && PLANNED.some((p) => p.path === to.path)) return '/'
  return true
})

export default router
