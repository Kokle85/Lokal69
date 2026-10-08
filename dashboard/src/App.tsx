import { lazy, type ComponentType } from 'react'
import { useParams, type RouteObject } from 'react-router'
import { useAuth } from './auth/AuthProvider'
import { AuthCallbackScreen, LoginScreen, RequireAuth } from './auth/screens'
import { Layout } from './components/Layout'
import { NotFoundScreen } from './screens/MiscScreens'
import { WorkspaceGate } from './workspace/WorkspaceProvider'

/**
 * Screens are code-split: each is its own chunk, loaded on first visit (the sign-in screens and
 * the shell stay in the initial bundle with supabase-js, which the session restore needs at once).
 * `Layout` wraps the outlet in a Suspense boundary with a loading state and a reload path when a
 * chunk cannot be fetched.
 */
function screen<K extends string>(load: () => Promise<Record<K, ComponentType>>, name: K) {
  return lazy(async () => ({ default: (await load())[name] }))
}

const OverviewScreen = screen(() => import('./screens/OverviewScreen'), 'OverviewScreen')
const CandidatesScreen = screen(() => import('./screens/CandidatesScreen'), 'CandidatesScreen')
const CandidateDetailScreen = screen(() => import('./screens/CandidateDetailScreen'), 'CandidateDetailScreen')
const EconomicsScreen = screen(() => import('./screens/EconomicsScreen'), 'EconomicsScreen')
const ReviewQueueScreen = screen(() => import('./screens/ReviewQueueScreen'), 'ReviewQueueScreen')
const ReviewCaseScreen = screen(() => import('./screens/ReviewCaseScreen'), 'ReviewCaseScreen')
const SourcesScreen = screen(() => import('./screens/SourcesScreen'), 'SourcesScreen')
const SettingsScreen = screen(() => import('./screens/SettingsScreen'), 'SettingsScreen')
const LagsScreen = screen(() => import('./screens/LagsScreen'), 'LagsScreen')
const ListingLifecycleScreen = screen(() => import('./screens/LagsScreen'), 'ListingLifecycleScreen')
const EvaluationScreen = screen(() => import('./screens/EvaluationScreen'), 'EvaluationScreen')
const InquiriesScreen = screen(() => import('./screens/inquiries/InquiriesScreen'), 'InquiriesScreen')
const InquiryDetailScreen = screen(() => import('./screens/inquiries/InquiryDetailScreen'), 'InquiryDetailScreen')
const RepliesScreen = screen(() => import('./screens/inquiries/RepliesScreen'), 'RepliesScreen')
const ReplyDetailScreen = screen(() => import('./screens/inquiries/ReplyDetailScreen'), 'ReplyDetailScreen')
const InquiryControlScreen = screen(() => import('./screens/inquiries/InquiryControlScreen'), 'InquiryControlScreen')
const MailWorkersScreen = screen(() => import('./screens/inquiries/MailWorkersScreen'), 'MailWorkersScreen')

/** Everything below is remounted when the signed-in user changes, so no state leaks across users. */
function UserScope() {
  const { userId } = useAuth()
  return (
    <WorkspaceGate key={userId ?? 'anonymous'}>
      <Layout />
    </WorkspaceGate>
  )
}

/**
 * Per-object screens are remounted when their id changes (e.g. back/forward between two cases), so
 * a draft, an unconfirmed mutation or a reload marker of one case can never carry over to another.
 */
function ReviewCaseRoute() {
  const { caseId = '' } = useParams()
  return <ReviewCaseScreen key={caseId} />
}

function CandidateDetailRoute() {
  const { listingId = '' } = useParams()
  return <CandidateDetailScreen key={listingId} />
}

function InquiryDetailRoute() {
  const { inquiryId = '' } = useParams()
  return <InquiryDetailScreen key={inquiryId} />
}

function ReplyDetailRoute() {
  const { replyId = '' } = useParams()
  return <ReplyDetailScreen key={replyId} />
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
      { path: 'candidates/:listingId', element: <CandidateDetailRoute /> },
      { path: 'candidates/:listingId/economics', element: <EconomicsScreen /> },
      { path: 'candidates/:listingId/lifecycle', element: <ListingLifecycleScreen /> },
      { path: 'valuations/:valuationId', element: <EconomicsScreen /> },
      { path: 'reviews', element: <ReviewQueueScreen /> },
      { path: 'reviews/:caseId', element: <ReviewCaseRoute /> },
      { path: 'sources', element: <SourcesScreen /> },
      { path: 'settings', element: <SettingsScreen /> },
      { path: 'lifecycle', element: <LagsScreen /> },
      // Spec v1.1 seller inquiries (no approve or send action exists anywhere: spec 37.1).
      { path: 'inquiries', element: <InquiriesScreen /> },
      { path: 'inquiries/:inquiryId', element: <InquiryDetailRoute /> },
      // The owner's Slack links (seller-reply signal, "decision needed" alert) use this form
      // (`domain.replies.dashboard_reply_url`). The page loads the reply by its own id only; the
      // inquiry shown is the one the server returns for it, never the one named in the link.
      { path: 'inquiries/:inquiryId/replies/:replyId', element: <ReplyDetailRoute /> },
      { path: 'replies', element: <RepliesScreen /> },
      { path: 'replies/:replyId', element: <ReplyDetailRoute /> },
      { path: 'inquiry-control', element: <InquiryControlScreen /> },
      { path: 'mail-workers', element: <MailWorkersScreen /> },
      { path: 'evaluation', element: <EvaluationScreen /> },
      { path: '*', element: <NotFoundScreen /> },
    ],
  },
]
