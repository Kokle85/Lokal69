import { Link } from 'react-router'
import { Notice } from '../components/ui'

/** Placeholder until the v1.1 seller-inquiry routes are part of docs/api_contract.md. */
export function InquiriesPlaceholder() {
  return (
    <div className="screen">
      <h1>Seller inquiries (coming with v1.1 wiring)</h1>
      <Notice tone="info">
        The bounded seller-inquiry and reply screens (spec 37) arrive in a later package, once their API routes are part of the
        dashboard contract. Nothing on this page sends or reads email.
      </Notice>
      <p>
        Lifecycle and coverage evidence that the API already offers is shown on the <Link to="/">overview</Link> (coverage
        gaps, last successful scans), on each candidate (first/last seen, detail and availability checks, availability history)
        and on <Link to="/sources">sources</Link> (run history).
      </p>
    </div>
  )
}

export function NotFoundScreen() {
  return (
    <div className="screen">
      <h1>Page not found</h1>
      <p>
        <Link to="/">Back to the overview</Link>
      </p>
    </div>
  )
}
