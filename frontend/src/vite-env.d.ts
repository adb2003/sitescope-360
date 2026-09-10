/// <reference types="vite/client" />

/**
 * Typed build-time configuration. Declared explicitly rather than relying on the loose index
 * signature `vite/client` provides, so a typo in an env var name is a compile error.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
