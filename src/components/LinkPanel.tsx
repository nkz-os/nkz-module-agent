/**
 * LinkPanel — account linking panel for the chat assistant.
 *
 * Generates a deep link to connect a messaging account, lists the caller's
 * active links, and revokes them. Consumes the three link-management routes
 * from the backend (Task 11): POST /api/agent/link-tokens, GET /api/agent/links,
 * DELETE /api/agent/links/{id}.
 *
 * Styling: plain elements with Tailwind utility classes — no
 * `@nekazari/ui-kit` components. Checked and rejected:
 *   - `Stack`/`Inline` exist only in the unpublished monorepo source of
 *     ui-kit; the resolved npm version (`^1.0.0` -> 1.0.1, semver excludes
 *     the newer prerelease) ships only `Button` and `Card`.
 *   - `Button` itself was avoided too: that resolved version ships no type
 *     declarations (would fail `tsc --noEmit` under `strict`) and hardcodes
 *     non-token green colors that clash with this module's accent.
 * Every class below was confirmed present in the platform host's own source
 * (`nkz/apps/host/src`), so Tailwind emits it in the host bundle even though
 * this module's source is never scanned; the `nkz-*` design-token classes
 * are additionally covered by the host's `safelist: [{ pattern: /-nkz-/ }]`.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

const API_BASE = import.meta.env?.VITE_API_URL || '';

interface Link {
  id: number;
  channel: string;
  channel_user_id: string;
  linked_at: string;
}

export function LinkPanel() {
  const { t } = useTranslation('agent');
  const [links, setLinks] = useState<Link[]>([]);
  const [deepLink, setDeepLink] = useState<string | null>(null);
  const [expiresIn, setExpiresIn] = useState(0);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    setError(false);
    try {
      const res = await fetch(`${API_BASE}/api/agent/links`, { credentials: 'include' });
      if (!res.ok) return setError(true);
      setLinks((await res.json()).links);
    } catch {
      setError(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const generate = async () => {
    setError(false);
    try {
      const res = await fetch(`${API_BASE}/api/agent/link-tokens`, {
        method: 'POST',
        credentials: 'include',
      });
      if (!res.ok) {
        // A failed regenerate does not invalidate the previously minted link
        // (it is still valid server-side), but its expiry text on screen is
        // a snapshot, not a live countdown — leaving it up would read as a
        // fresh result. Clear it so the error banner is the only thing shown.
        setDeepLink(null);
        return setError(true);
      }
      const body = await res.json();
      setDeepLink(body.deep_link);
      setExpiresIn(Math.round(body.expires_in / 60));
    } catch {
      setDeepLink(null);
      setError(true);
    }
  };

  const revoke = async (id: number) => {
    setError(false);
    try {
      const res = await fetch(`${API_BASE}/api/agent/links/${id}`, {
        method: 'DELETE',
        credentials: 'include',
      });
      if (!res.ok) return setError(true);
      await load();
    } catch {
      setError(true);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <p className="text-nkz-text-secondary">{t('intro')}</p>

      <button
        className="self-start bg-nkz-accent-base text-nkz-text-on-accent rounded-nkz-md px-4 py-2 text-nkz-sm font-medium"
        onClick={generate}
      >
        {t('generate')}
      </button>

      {deepLink && (
        <div className="flex flex-col gap-1">
          <a
            href={deepLink}
            target="_blank"
            rel="noreferrer"
            className="text-nkz-accent-base underline text-nkz-sm"
          >
            {t('openLink')}
          </a>
          <span className="text-nkz-text-muted text-nkz-sm">
            {t('expiresIn', { minutes: expiresIn })}
          </span>
        </div>
      )}

      <h3 className="text-nkz-text-primary font-medium">{t('linked')}</h3>

      {links.length === 0 ? (
        <p className="text-nkz-text-muted">{t('none')}</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {links.map((l) => (
            <li key={l.id} className="flex items-center justify-between gap-4">
              <span className="text-nkz-text-primary">
                {l.channel} · {l.channel_user_id}
              </span>
              <button
                className="text-nkz-danger text-nkz-sm underline"
                onClick={() => revoke(l.id)}
              >
                {t('revoke')}
              </button>
            </li>
          ))}
        </ul>
      )}

      {error && <p className="text-nkz-danger">{t('error')}</p>}
    </div>
  );
}
