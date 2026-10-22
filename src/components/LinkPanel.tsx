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
 * (`nkz/apps/host/src`, plus `packages/{ui-kit,viewer-kit}/src` — the same
 * three trees the host's own `tailwind.config.js` `content` scans), so
 * Tailwind emits it in the host bundle even though this module's source is
 * never scanned; the `nkz-*` design-token classes are additionally covered
 * by the host's `safelist: [{ pattern: /-nkz-/ }]`.
 *
 * QR code: rendered with `qrcode-generator` (MIT, zero dependencies) — it
 * only computes the module matrix; this component draws that matrix as a
 * single inline `<path>` (no canvas, no dangerouslySetInnerHTML). Not a
 * federation shared singleton, so it bundles into this module's own chunk.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import qrcode from 'qrcode-generator';

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
  const [copied, setCopied] = useState(false);

  // The API deliberately never returns a raw `token` field (see
  // test_response_never_exposes_the_raw_token_field in the backend) — the
  // token only ever travels inside the deep link's `start` query param.
  // Reading it back out here doesn't expose anything new to the page: the
  // full URL, token included, is already in `deepLink`.
  const token = useMemo(() => {
    if (!deepLink) return null;
    try {
      return new URL(deepLink).searchParams.get('start');
    } catch {
      return null;
    }
  }, [deepLink]);

  // Draws the QR as one inline SVG <path> (no canvas, no innerHTML) so a
  // desktop user with no messaging app installed can scan it from a phone
  // instead of hitting a dead deep link — the failure this panel exists to fix.
  const qrPath = useMemo(() => {
    if (!deepLink) return null;
    const qr = qrcode(0, 'M');
    qr.addData(deepLink);
    qr.make();
    const count = qr.getModuleCount();
    let d = '';
    for (let row = 0; row < count; row++) {
      for (let col = 0; col < count; col++) {
        if (qr.isDark(row, col)) d += `M${col},${row}h1v1h-1z`;
      }
    }
    return { d, count };
  }, [deepLink]);

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
    setCopied(false);
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

  const copyToken = async () => {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
    } catch {
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
      <p className="text-nkz-text-secondary text-sm">{t('howItWorks')}</p>

      <button
        className="self-start bg-nkz-accent-base text-nkz-text-on-accent rounded-nkz-md px-4 py-2 text-nkz-sm font-medium"
        onClick={generate}
      >
        {t('generate')}
      </button>

      {deepLink && (
        <div className="flex flex-col gap-4">
          {qrPath && (
            <div className="flex flex-col gap-1">
              <span className="text-nkz-text-muted text-xs">{t('scanQr')}</span>
              <div className="inline-block self-start bg-white p-3 rounded-md">
                <svg
                  viewBox={`0 0 ${qrPath.count} ${qrPath.count}`}
                  width={160}
                  height={160}
                  role="img"
                  aria-label={t('qrAlt')}
                >
                  <path d={qrPath.d} fill="#000000" />
                </svg>
              </div>
            </div>
          )}

          <a
            href={deepLink}
            target="_blank"
            rel="noreferrer"
            className="self-start text-nkz-accent-base underline text-nkz-sm"
          >
            {t('openLink')}
          </a>

          {token && (
            <div className="flex flex-col gap-1">
              <span className="text-nkz-text-muted text-xs">{t('fallbackInstruction')}</span>
              <div className="flex items-center gap-2 flex-wrap">
                <code className="font-mono text-sm text-nkz-text-primary break-all bg-nkz-surface-sunken rounded px-2 py-1">
                  {token}
                </code>
                <button
                  className="shrink-0 text-nkz-accent-base underline text-nkz-sm"
                  onClick={copyToken}
                >
                  {copied ? t('copied') : t('copyToken')}
                </button>
              </div>
            </div>
          )}

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
