/**
 * Global type declarations for the Nekazari host runtime.
 * In Module Federation 2.0, shared dependencies (React, SDK, etc.) are
 * resolved as federation singletons — not via window globals.
 *
 * This file is retained for dev-mode (Vite MockProvider) and for modules
 * that access CesiumJS via the global scope.
 */

declare global {
  interface Window {
    /** CesiumJS — available in the map viewer context */
    Cesium?: unknown;
    /** Runtime env vars injected by host entrypoint */
    __ENV__?: Record<string, string>;
  }
}

export {};
