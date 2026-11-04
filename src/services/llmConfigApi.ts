/**
 * Client for the tenant LLM provider and usage routes (TenantAdmin only).
 * Same conventions as LinkPanel: API origin from the host page at runtime,
 * `credentials: 'include'`, never a relative `/api`.
 */
const API_ORIGIN = (
  (window as unknown as { __ENV__?: { VITE_API_URL?: string } }).__ENV__?.VITE_API_URL ??
  import.meta.env?.VITE_API_URL ??
  ''
).replace(/\/$/, '');

const BASE = `${API_ORIGIN}/api/agent`;

export interface AllowedEntry {
  provider: string;
  origin: string | null;
}

export interface LlmConfig {
  byok_available: boolean;
  configured: boolean;
  provider: string | null;
  model: string | null;
  api_base: string | null;
  key_last4: string | null;
  max_tokens_per_turn: number | null;
  ceiling: number;
  allowed: AllowedEntry[];
  notice: string | null;
}

export interface LlmConfigInput {
  provider: string;
  model: string;
  api_base?: string | null;
  api_key?: string;
  max_tokens_per_turn?: number | null;
}

export interface UsageDay {
  date: string;
  turns: number;
  tokens_prompt: number;
  tokens_completion: number;
  cost_estimate_usd: number | null;
}

export interface Usage {
  month: string;
  days: UsageDay[];
  totals: Omit<UsageDay, 'date'>;
}

/** `forbidden` = 401/403: the caller is not an admin. `code` = backend detail.code. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string | null,
  ) {
    super(code ?? `http_${status}`);
  }
  get forbidden(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
  });
  if (!res.ok) {
    let code: string | null = null;
    try {
      const detail = (await res.json())?.detail;
      code = typeof detail?.code === 'string' ? detail.code : null;
    } catch {
      /* no JSON body */
    }
    throw new ApiError(res.status, code);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const getLlmConfig = () => call<LlmConfig>('/llm-config');
export const putLlmConfig = (body: LlmConfigInput) =>
  call<LlmConfig>('/llm-config', { method: 'PUT', body: JSON.stringify(body) });
export const deleteLlmConfig = () => call<void>('/llm-config', { method: 'DELETE' });
export const testLlmConfig = () =>
  call<{ ok: boolean; error?: string }>('/llm-config/test', { method: 'POST' });
export const getUsage = () => call<Usage>('/usage');
