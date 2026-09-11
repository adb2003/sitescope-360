import { defineConfig } from 'vitest/config'

/**
 * Node-environment tests for the app's pure TypeScript.
 *
 * Scoped to `src/lib/**` on purpose. Those modules — the OSC camera client above all — take
 * their side effects as injected dependencies, so they run under plain Node with no React Native
 * runtime, no Metro bundler and no device. Screens are not covered here: testing them would mean
 * standing up the RN renderer, which is a different and much heavier undertaking.
 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/lib/**/*.test.ts'],
  },
})
