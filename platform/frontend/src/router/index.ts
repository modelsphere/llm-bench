import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/benchmarks',
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/Login.vue'),
      meta: { guest: true },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/Register.vue'),
      meta: { guest: true },
    },
    {
      path: '/forgot-password',
      name: 'forgot-password',
      component: () => import('@/views/ForgotPassword.vue'),
      meta: { guest: true },
    },
    {
      // Landing page for an approved reset link: /reset-password?token=…
      path: '/reset-password',
      name: 'reset-password',
      component: () => import('@/views/ResetPassword.vue'),
      meta: { guest: true },
    },
    {
      path: '/benchmarks',
      name: 'benchmark-list',
      component: () => import('@/views/BenchmarkList.vue'),
      meta: { auth: true },
    },
    {
      path: '/benchmarks/:slug',
      name: 'benchmark-detail',
      component: () => import('@/views/BenchmarkDetail.vue'),
      meta: { auth: true },
    },
    {
      path: '/benchmarks/:slug/submit',
      name: 'benchmark-submit',
      component: () => import('@/views/BenchmarkSubmit.vue'),
      meta: { auth: true },
    },
    {
      path: '/modules/:name/submit',
      name: 'module-submit',
      component: () => import('@/views/ModuleSubmit.vue'),
      meta: { auth: true },
    },
    {
      path: '/submissions/:id',
      name: 'submission-detail',
      component: () => import('@/views/SubmissionDetail.vue'),
      meta: { auth: true },
    },
    {
      path: '/submissions',
      name: 'my-submissions',
      component: () => import('@/views/MySubmissions.vue'),
      meta: { auth: true },
    },
    {
      path: '/account/password',
      name: 'change-password',
      component: () => import('@/views/ChangePassword.vue'),
      meta: { auth: true },
    },
    {
      path: '/admin/benchmarks',
      name: 'admin-benchmark-list',
      component: () => import('@/views/admin/BenchmarkList.vue'),
      meta: { auth: true, admin: true },
    },
    {
      path: '/admin/benchmarks/new',
      name: 'benchmark-create',
      component: () => import('@/views/admin/BenchmarkEditor.vue'),
      meta: { auth: true, admin: true },
    },
    {
      path: '/admin/benchmarks/:id',
      name: 'benchmark-edit',
      component: () => import('@/views/admin/BenchmarkEditor.vue'),
      meta: { auth: true, admin: true },
    },
    {
      path: '/admin/version',
      name: 'admin-version',
      component: () => import('@/views/admin/SystemVersion.vue'),
      meta: { auth: true, admin: true },
    },
    {
      path: '/admin/users',
      name: 'admin-users',
      component: () => import('@/views/admin/Users.vue'),
      meta: { auth: true, admin: true },
    },
    {
      path: '/admin/submissions',
      name: 'admin-submissions',
      component: () => import('@/views/admin/AllSubmissions.vue'),
      meta: { auth: true, admin: true },
    },
    {
      path: '/admin/card-types',
      name: 'admin-card-types',
      component: () => import('@/views/admin/CardTypes.vue'),
      meta: { auth: true, admin: true },
    },
    {
      path: '/admin/replay-datasets',
      name: 'admin-replay-datasets',
      component: () => import('@/views/admin/ReplayDatasets.vue'),
      meta: { auth: true, admin: true },
    },
    {
      path: '/account/api-keys',
      name: 'api-keys',
      component: () => import('@/views/ApiKeys.vue'),
      meta: { auth: true },
    },
    // Catch-all 404 — must stay last. No auth meta so a wrong URL shows the
    // 404 page rather than bouncing to /login.
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFound.vue'),
    },
  ],
})

router.beforeEach((to, _from, next) => {
  const auth = useAuthStore()

  if (to.meta.auth && !auth.isAuthenticated) {
    next({ name: 'login', query: { redirect: to.fullPath } })
  } else if (to.meta.admin && !auth.isAdmin) {
    next({ name: 'benchmark-list' })
  } else if (to.meta.guest && auth.isAuthenticated) {
    next({ name: 'benchmark-list' })
  } else {
    next()
  }
})

export default router
