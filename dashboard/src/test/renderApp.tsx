import { render } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { vi } from 'vitest'
import { routes } from '../App'
import { AuthProvider } from '../auth/AuthProvider'
import type { FakeApi } from './fakeApi'
import { FakeAuth } from './fakeAuth'

/** Render the real route tree in memory with a fake auth client and a fake API behind `fetch`. */
export function renderApp(path: string, options: { auth?: FakeAuth; api?: FakeApi } = {}) {
  const auth = options.auth ?? new FakeAuth(true)
  if (options.api) vi.stubGlobal('fetch', options.api.fetch)
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  const result = render(
    <AuthProvider auth={auth}>
      <RouterProvider router={router} />
    </AuthProvider>,
  )
  return { ...result, router, auth }
}
