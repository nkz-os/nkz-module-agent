import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import en from '../locales/en.json';
import es from '../locales/es.json';

// Resolve keys against the real English bundle so copy and keys are tested together.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) => {
      const found = key
        .split('.')
        .reduce<unknown>((o, k) => (o as Record<string, unknown> | undefined)?.[k], en);
      if (typeof found !== 'string') return `MISSING:${key}`;
      return found.replace(/\{\{(\w+)\}\}/g, (_, k) => String(vars?.[k] ?? ''));
    },
  }),
}));

import { LlmConfigPanel } from './LlmConfigPanel';

const ORIGIN = 'https://llm.example.test';
const KEY = 'sk-synthetic-secret-1234';

const base = {
  byok_available: true,
  configured: false,
  provider: null,
  model: null,
  api_base: null,
  key_last4: null,
  max_tokens_per_turn: null,
  ceiling: 4000,
  allowed: [
    { provider: 'provider-a', origin: null },
    { provider: 'provider-b', origin: ORIGIN },
  ],
  notice: null,
};
const configured = {
  ...base,
  configured: true,
  provider: 'provider-a',
  model: 'model-x',
  key_last4: '1234',
};
const usage = {
  month: '2026-11',
  days: [
    { date: '2026-11-01', turns: 3, tokens_prompt: 100, tokens_completion: 50, cost_estimate_usd: null },
    { date: '2026-11-02', turns: 1, tokens_prompt: 10, tokens_completion: 5, cost_estimate_usd: 0.5 },
  ],
  totals: { turns: 4, tokens_prompt: 110, tokens_completion: 55, cost_estimate_usd: null },
};

type Handler = (init?: RequestInit) => { status: number; body?: unknown };
let routes: Record<string, Handler>;
let calls: { url: string; init?: RequestInit }[];

const json = (status: number, body?: unknown) => ({ status, body });

beforeEach(() => {
  calls = [];
  routes = {};
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const key = `${init?.method ?? 'GET'} ${url.replace(/^.*\/api\/agent/, '')}`;
      const r = (routes[key] ?? (() => json(404)))(init);
      return {
        ok: r.status >= 200 && r.status < 300,
        status: r.status,
        json: async () => r.body,
      } as Response;
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('LlmConfigPanel', () => {
  it('hides everything for non-admins (403)', async () => {
    routes['GET /llm-config'] = () => json(403, { detail: 'forbidden' });
    const { container } = render(<LlmConfigPanel />);
    await waitFor(() => expect(calls.length).toBe(1));
    expect(container.innerHTML).toBe('');
    expect(calls.some((c) => c.url.includes('/usage'))).toBe(false);
  });

  it('says it is not available when byok_available is false', async () => {
    routes['GET /llm-config'] = () => json(200, { ...base, byok_available: false });
    routes['GET /usage'] = () => json(200, usage);
    render(<LlmConfigPanel />);
    expect(await screen.findByText(en.llm.unavailable)).toBeTruthy();
    expect(screen.queryByText(en.llm.save)).toBeNull();
  });

  it('shows the not-configured notice and the organisation copy', async () => {
    routes['GET /llm-config'] = () => json(200, base);
    routes['GET /usage'] = () => json(200, usage);
    render(<LlmConfigPanel />);
    expect(await screen.findByText(en.llm.notConfigured)).toBeTruthy();
    expect(screen.getByText(en.llm.intro)).toBeTruthy();
    expect(screen.getByText(en.llm.keyNote)).toBeTruthy();
  });

  it('uses a password key field, sends the key once, then clears it and shows only last4', async () => {
    routes['GET /llm-config'] = () => json(200, base);
    routes['GET /usage'] = () => json(200, usage);
    routes['PUT /llm-config'] = () => json(200, configured);
    const { container } = render(<LlmConfigPanel />);
    await screen.findByText(en.llm.save);
    const keyInput = container.querySelector('input[type="password"]') as HTMLInputElement;
    expect(keyInput).toBeTruthy();
    const model = screen.getByLabelText(en.llm.model) as HTMLInputElement;
    fireEvent.change(model, { target: { value: 'model-x' } });
    fireEvent.change(keyInput, { target: { value: KEY } });
    fireEvent.click(screen.getByText(en.llm.save));
    await screen.findByText(en.llm.saved);

    const put = calls.find((c) => c.init?.method === 'PUT')!;
    expect(put.url.startsWith('/api/agent') || put.url.includes('/api/agent/llm-config')).toBe(true);
    expect(put.init?.credentials).toBe('include');
    expect(JSON.parse(put.init!.body as string)).toMatchObject({
      provider: 'provider-a',
      model: 'model-x',
      api_key: KEY,
    });
    expect(keyInput.value).toBe('');
    expect(container.innerHTML).not.toContain(KEY);
    expect(screen.getByText('Saved key: …1234')).toBeTruthy();
  });

  it('changing provider clears the key and requires a new one', async () => {
    routes['GET /llm-config'] = () => json(200, configured);
    routes['GET /usage'] = () => json(200, usage);
    const { container } = render(<LlmConfigPanel />);
    await screen.findByText(en.llm.keyKeep);
    const keyInput = container.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(keyInput, { target: { value: KEY } });
    fireEvent.change(screen.getByLabelText(en.llm.provider), {
      target: { value: `provider-b@${ORIGIN}` },
    });
    expect(keyInput.value).toBe('');
    expect(screen.getByText(en.llm.keyRequired)).toBeTruthy();
    // origin entry prefills and hints the endpoint
    expect((screen.getByLabelText(new RegExp(en.llm.apiBase)) as HTMLInputElement).value).toBe(ORIGIN);
    fireEvent.click(screen.getByText(en.llm.save));
    expect(await screen.findByText(en.llm.errors.key_required)).toBeTruthy();
    expect(calls.some((c) => c.init?.method === 'PUT')).toBe(false);
  });

  it.each(Object.keys(en.llm.errors))('maps backend code %s to its message', async (code) => {
    routes['GET /llm-config'] = () => json(200, configured);
    routes['GET /usage'] = () => json(200, usage);
    routes['PUT /llm-config'] = () => json(422, { detail: { code } });
    render(<LlmConfigPanel />);
    await screen.findByText(en.llm.save);
    fireEvent.click(screen.getByText(en.llm.save));
    expect(
      await screen.findByText((en.llm.errors as Record<string, string>)[code]),
    ).toBeTruthy();
  });

  it('shows "—" for null cost and labels cost as estimate (USD)', async () => {
    routes['GET /llm-config'] = () => json(200, base);
    routes['GET /usage'] = () => json(200, usage);
    render(<LlmConfigPanel />);
    expect(await screen.findByText(en.usage.cost)).toBeTruthy();
    expect(en.usage.cost).toBe('Cost, estimate (USD)');
    expect(screen.getAllByText('—').length).toBe(2); // day 1 and total
    expect(screen.getByText('0.5000')).toBeTruthy();
  });
});

describe('locales', () => {
  const flat = (o: unknown, p = ''): string[] =>
    Object.entries(o as Record<string, unknown>).flatMap(([k, v]) =>
      typeof v === 'object' && v ? flat(v, `${p}${k}.`) : [`${p}${k}`],
    );
  it('es and en have the same keys', () => {
    expect(flat(es).sort()).toEqual(flat(en).sort());
  });
});
