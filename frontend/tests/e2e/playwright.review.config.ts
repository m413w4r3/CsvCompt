import { defineConfig, devices } from '@playwright/test'

/**
 * Configuration de l'E2E de revue : le build de production est servi par `vite preview`, avec la CSP
 * d'exécution (script-src 'self'). Le serveur de développement injecte un script inline de
 * rafraîchissement que cette CSP bloque ; le build n'en contient pas.
 */
export default defineConfig({
  testDir: '.',
  testMatch: 'review.spec.ts',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:4173' },
  webServer: {
    command: 'npm run preview -- --port 4173 --strictPort',
    cwd: new URL('../..', import.meta.url).pathname,
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
    timeout: 60_000,
  },
})
