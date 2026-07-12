import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// During `npm run dev`, proxy every backend route to the FastAPI dev server
// (uvicorn web.app:app --reload --port 8000) so relative fetch() calls work
// the same in dev as they do in production, where FastAPI serves the built
// SPA and API from the same origin.
const BACKEND = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    // Bind explicitly to the IPv4 loopback, not the string "localhost" (which
    // on some setups resolves to the IPv6 ::1 instead and silently makes
    // 127.0.0.1 unreachable). This matters here specifically because the
    // Spotify OAuth session cookie is scoped to whatever host the browser
    // used - if the frontend were reachable at a different host than
    // 127.0.0.1:8000 (where SPOTIFY_WEB_REDIRECT_URI sends the browser back
    // to after login), the cookie set during /login wouldn't be sent back
    // on /callback, breaking the login flow.
    host: '127.0.0.1',
    proxy: {
      '/login': BACKEND,
      '/callback': BACKEND,
      '/logout': BACKEND,
      '/me': BACKEND,
      '/scan': BACKEND,
      '/jobs': BACKEND,
      '/status': BACKEND,
      '/download': BACKEND,
      '/healthz': BACKEND,
    },
  },
})
