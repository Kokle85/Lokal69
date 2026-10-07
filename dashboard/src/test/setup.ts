import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  try {
    window.sessionStorage.clear()
    window.localStorage.clear()
  } catch {
    // ignore
  }
})
