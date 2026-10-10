import { Link } from 'react-router'

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
