/**
 * API Client for Agent
 *
 * This is a template for creating an API client that uses the Nekazari SDK.
 * Replace with your actual API endpoints and methods.
 */

import { NKZClient, useAuth } from '@nekazari/sdk';

// The frontend host and the API can live on different domains (MF2 modules
// run inside the host's origin while the API is served from a separate
// host) — a relative baseUrl silently breaks cross-origin. There is NO useConfig() hook in
// @nekazari/sdk; read the API base at build time via VITE_API_URL, with a
// placeholder fallback so this never silently points at a real domain.
const API_BASE = (import.meta as any).env?.VITE_API_URL || 'https://your-api-domain';

/**
 * Hook to get API client instance
 * Automatically handles authentication and tenant context
 */
export function useModuleApi() {
  const { getToken, getTenantId } = useAuth();

  const client = new NKZClient({
    baseUrl: `${API_BASE}/api/agent`,
    getToken,
    getTenantId,
  });

  return {
    // Example API methods - replace with your actual endpoints
    getData: () => client.get('/data'),
    getDataById: (id: string) => client.get(`/data/${id}`),
    createData: (data: any) => client.post('/data', data),
    updateData: (id: string, data: any) => client.put(`/data/${id}`, data),
    deleteData: (id: string) => client.delete(`/data/${id}`),
  };
}

/**
 * Standalone API client (for use outside React components)
 */
export function createModuleApiClient() {
  // This would need token and tenantId passed in
  // For React components, use useModuleApi() instead
  throw new Error('Use useModuleApi() hook in React components');
}

