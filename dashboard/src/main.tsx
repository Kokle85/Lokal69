import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter } from 'react-router'
import { RouterProvider } from 'react-router/dom'
import { routes } from './App'
import { AuthProvider } from './auth/AuthProvider'
import { supabaseAuth } from './auth/authClient'
import { readConfig } from './config'
import './styles.css'

const root = createRoot(document.getElementById('root') as HTMLElement)
const config = readConfig()

if (!config.ok) {
  root.render(
    <StrictMode>
      <main className="centered-page" id="main">
        <h1>Dashboard not configured</h1>
        <p>The build is missing its public configuration:</p>
        <ul>
          {config.problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
        <p>See dashboard/README.md (environment variables).</p>
      </main>
    </StrictMode>,
  )
} else {
  const router = createBrowserRouter(routes)
  root.render(
    <StrictMode>
      <AuthProvider auth={supabaseAuth(config.config)}>
        <RouterProvider router={router} />
      </AuthProvider>
    </StrictMode>,
  )
}
