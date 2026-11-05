import { lazy, Suspense } from 'react'
import { BrowserRouter, HashRouter, Route, Routes } from 'react-router-dom'
import { useCheckBackend } from '@/hooks/useCheckBackend'
import { systemCheck } from '@/services/system'
import { StartupScreen } from '@/ember/startup/StartupScreen'
import { StartupNotice } from '@/ember/startup/StartupNotice'
import { WorkspacePage } from '@/ember/pages/WorkspacePage'
import { OnboardingGuard } from '@/ember/pages/OnboardingGuard'
import { NotFoundPage } from '@/ember/pages/NotFoundPage'
import { Spinner } from '@/ember/ui/Spinner'
import { useEffect } from 'react'

const SettingsPage = lazy(() => import('@/ember/pages/settings/SettingsPage'))
// Lazy so the browser build never downloads the desktop-only setup flow.
const OnboardingPage = lazy(() =>
  import('@/ember/pages/OnboardingPage').then(module => ({ default: module.OnboardingPage })),
)

/**
 * Ember application root. Keeps the two long-standing runtime concerns in one
 * place — waiting for the backend to initialise, and running the system check
 * once it has — and hands everything else to the router.
 */
export function EmberApp() {
  const { loading, initialized, failed, lastError, reason, retry } = useCheckBackend()

  useEffect(() => {
    if (initialized) void systemCheck()
  }, [initialized])

  if (!initialized) {
    return (
      <>
        <StartupNotice />
        <StartupScreen
          loading={loading}
          failed={failed}
          lastError={lastError}
          reason={reason}
          onRetry={retry}
        />
      </>
    )
  }

  const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  const Router = isTauri ? HashRouter : BrowserRouter

  return (
    <>
      <StartupNotice />
      <Router>
        <Suspense
          fallback={
            <div className="grid h-screen place-items-center">
              <Spinner className="size-5 text-ember" />
            </div>
          }
        >
          <Routes>
            <Route path="/onboarding" element={<OnboardingPage />} />
            <Route
              path="/"
              element={
                <OnboardingGuard>
                  <WorkspacePage />
                </OnboardingGuard>
              }
            />
            <Route path="/settings/*" element={<SettingsPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </Suspense>
      </Router>
    </>
  )
}
