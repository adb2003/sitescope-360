import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { App } from '@/App'
import { ApiError } from '@/lib/api'
import { AuthProvider } from '@/hooks/useAuth'
import { ProjectContextProvider } from '@/hooks/useProjectContext'
import '@/styles/index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: (attempt, error) => {
        // Never retry a 4xx: a 403 will stay a 403, and retrying a 401 fights the token refresh
        // the API client already performs.
        if (error instanceof ApiError && error.status < 500) return false
        return attempt < 2
      },
    },
  },
})

const container = document.getElementById('root')
if (!container) throw new Error('#root is missing from index.html')

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ProjectContextProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ProjectContextProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
