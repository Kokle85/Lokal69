import type { RouteObject } from 'react-router'
import { useAuth } from './auth/AuthProvider'
import { AuthCallbackScreen, LoginScreen, RequireAuth } from './auth/screens'
import { Layout } from './components/Layout'
import { CandidateDetailScreen } from './screens/CandidateDetailScreen'
import { CandidatesScreen } from './screens/CandidatesScreen'
import { EconomicsScreen } from './screens/EconomicsScreen'
import { InquiriesPlaceholder, NotFoundScreen } from './screens/MiscScreens'
import { OverviewScreen } from './screens/OverviewScreen'
import { ReviewCaseScreen } from './screens/ReviewCaseScreen'
import { ReviewQueueScreen } from './screens/ReviewQueueScreen'
import { SettingsScreen } from './screens/SettingsScreen'
import { SourcesScreen } from './screens/SourcesScreen'
import { WorkspaceGate } from './workspace/WorkspaceProvider'

/** Everything below is remounted when the signed-in user changes, so no state leaks across users. */
function UserScope() {
  const { userId } = useAuth()
  return (
    <WorkspaceGate key={userId ?? 'anonymous'}>
      <Layout />
    </WorkspaceGate>
  )
}

export const routes: RouteObject[] = [
  { path: '/login', element: <LoginScreen /> },
  { path: '/auth/callback', element: <AuthCallbackScreen /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <UserScope />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <OverviewScreen /> },
      { path: 'candidates', element: <CandidatesScreen /> },
      { path: 'candidates/:listingId', element: <CandidateDetailScreen /> },
      { path: 'candidates/:listingId/economics', element: <EconomicsScreen /> },
      { path: 'valuations/:valuationId', element: <EconomicsScreen /> },
      { path: 'reviews', element: <ReviewQueueScreen /> },
      { path: 'reviews/:caseId', element: <ReviewCaseScreen /> },
      { path: 'sources', element: <SourcesScreen /> },
      { path: 'settings', element: <SettingsScreen /> },
      { path: 'inquiries', element: <InquiriesPlaceholder /> },
      { path: '*', element: <NotFoundScreen /> },
    ],
  },
]
