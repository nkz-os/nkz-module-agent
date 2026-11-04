/**
 * LlmConfigPanel — the organisation's own AI provider for the assistant,
 * plus the usage table. TenantAdmin / PlatformAdmin only: a 401/403 on the
 * first GET hides both panels (the backend enforces the roles anyway).
 *
 * Plain elements and host classes, like LinkPanel (the resolved ui-kit
 * ships only Button/Card, without types or a Select). The API key lives in
 * component state only until the request is sent; it is cleared on every
 * outcome and never rendered back.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ApiError,
  deleteLlmConfig,
  getLlmConfig,
  putLlmConfig,
  testLlmConfig,
  type AllowedEntry,
  type LlmConfig,
} from '../services/llmConfigApi';
import { UsagePanel } from './UsagePanel';

const KNOWN_ERRORS = [
  'choose_provider',
  'byok_unavailable',
  'endpoint_not_allowed',
  'key_required',
  'budget_above_ceiling',
  'invalid_model',
  'invalid_key',
  'invalid_body',
];

/** Same identity the backend compares: scheme://host:port, default port explicit. */
function originOf(value: string | null | undefined): string | null {
  if (value == null) return null;
  try {
    const u = new URL(value.trim());
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return `invalid:${value}`;
    const port = u.port || (u.protocol === 'https:' ? '443' : '80');
    return `${u.protocol}//${u.hostname.toLowerCase()}:${port}`;
  } catch {
    return `invalid:${value}`;
  }
}

const NOTICES = ['blocked', 'key_unreadable'];
const TEST_ERRORS = [
  'not_configured',
  'provider_error',
  'auth_failed',
  'not_found',
  'quota_or_rate_limit',
  'bad_request',
  'unreachable',
];

const entryKey = (e: AllowedEntry) => `${e.provider}@${e.origin ?? ''}`;

const inputClass =
  'bg-nkz-surface-sunken text-nkz-text-primary rounded px-2 py-1 text-nkz-sm';

function initialEntry(cfg: LlmConfig): AllowedEntry | null {
  if (!cfg.configured) return cfg.allowed[0] ?? null;
  // Origin equality, like the backend: the stored api_base may carry a path
  // or omit the default port. No match -> nothing selected (never guess a
  // provider: the key would be saved under it).
  return (
    cfg.allowed.find(
      (e) =>
        e.provider === cfg.provider &&
        (e.origin === null ? !cfg.api_base : originOf(cfg.api_base) === e.origin),
    ) ?? null
  );
}

export function LlmConfigPanel() {
  const { t } = useTranslation('agent');
  const [cfg, setCfg] = useState<LlmConfig | null>(null);
  const [hidden, setHidden] = useState(false);
  const [loadError, setLoadError] = useState(false);

  const [entryId, setEntryId] = useState('');
  const [model, setModel] = useState('');
  const [apiBase, setApiBase] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [budget, setBudget] = useState('');
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string; detail?: string | null } | null>(null);

  const applyConfig = useCallback((c: LlmConfig) => {
    setCfg(c);
    const e = initialEntry(c);
    setEntryId(e ? entryKey(e) : '');
    setModel(c.model ?? '');
    setApiBase(c.api_base ?? '');
    setBudget(c.max_tokens_per_turn ? String(c.max_tokens_per_turn) : '');
    setApiKey('');
  }, []);

  useEffect(() => {
    getLlmConfig()
      .then(applyConfig)
      .catch((err) => {
        if (err instanceof ApiError && err.forbidden) setHidden(true);
        else setLoadError(true);
      });
  }, [applyConfig]);

  if (hidden) return null;
  if (loadError) return <p className="text-nkz-danger">{t('error')}</p>;
  if (!cfg) return null;

  const entry = cfg.allowed.find((e) => entryKey(e) === entryId) ?? null;
  const origin = entry?.origin ?? null;
  const providerChanged =
    cfg.configured &&
    (entry?.provider !== cfg.provider || originOf(origin !== null ? apiBase : null) !== originOf(cfg.api_base));
  const keyRequired = !cfg.configured || providerChanged;

  const errorText = (code: string) =>
    t(KNOWN_ERRORS.includes(code) ? `llm.errors.${code}` : 'error');

  const onProviderChange = (id: string) => {
    setEntryId(id);
    // Never prefill: the address must be typed, the origin is only a hint.
    setApiBase('');
    // A key belongs to one provider: never carry it over to another.
    setApiKey('');
    setSaved(false);
    setTestResult(null);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorCode(null);
    setSaved(false);
    setTestResult(null);
    if (!entry) return setErrorCode('choose_provider');
    if (origin !== null && originOf(apiBase) !== originOf(origin)) {
      return setErrorCode('endpoint_not_allowed');
    }
    if (keyRequired && !apiKey.trim()) return setErrorCode('key_required');
    const budgetNum = budget.trim() === '' ? null : Number(budget);
    if (budgetNum !== null && (!Number.isInteger(budgetNum) || budgetNum <= 0 || budgetNum > cfg.ceiling)) {
      return setErrorCode('budget_above_ceiling');
    }
    try {
      const next = await putLlmConfig({
        provider: entry.provider,
        model: model.trim(),
        api_base: origin !== null ? apiBase.trim() : null,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        max_tokens_per_turn: budgetNum,
      });
      applyConfig(next);
      setSaved(true);
    } catch (err) {
      setApiKey('');
      if (err instanceof ApiError && err.forbidden) return setHidden(true);
      setErrorCode(err instanceof ApiError ? (err.code ?? 'generic') : 'generic');
    }
  };

  const remove = async () => {
    setErrorCode(null);
    setSaved(false);
    setTestResult(null);
    try {
      await deleteLlmConfig();
      applyConfig(await getLlmConfig());
    } catch {
      setErrorCode('generic');
    }
  };

  const test = async () => {
    setErrorCode(null);
    setTestResult(null);
    try {
      setTestResult(await testLlmConfig());
    } catch {
      setErrorCode('generic');
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <h3 className="text-nkz-text-primary font-medium">{t('llm.title')}</h3>
      <p className="text-nkz-text-secondary">{t('llm.intro')}</p>
      <p className="text-nkz-text-secondary text-sm">{t('llm.keyNote')}</p>

      {!cfg.byok_available ? (
        <p className="text-nkz-danger">{t('llm.unavailable')}</p>
      ) : cfg.allowed.length === 0 ? (
        <p className="text-nkz-text-muted">{t('llm.noneAllowed')}</p>
      ) : (
        <>
          {!cfg.configured && <p className="text-nkz-text-muted">{t('llm.notConfigured')}</p>}
          {cfg.configured && !entry && <p className="text-nkz-danger">{t('llm.noMatch')}</p>}
          {cfg.notice && (
            <p className="text-nkz-danger">
              {NOTICES.includes(cfg.notice) ? t(`llm.notices.${cfg.notice}`) : t('llm.noticeGeneric')}
            </p>
          )}

          <form className="flex flex-col gap-4" onSubmit={save}>
            <label className="flex flex-col gap-1">
              <span className="text-nkz-text-muted text-xs">{t('llm.provider')}</span>
              <select
                className={inputClass}
                value={entryId}
                onChange={(ev) => onProviderChange(ev.target.value)}
              >
                {!entry && <option value="">{t('llm.choose')}</option>}
                {cfg.allowed.map((a) => (
                  <option key={entryKey(a)} value={entryKey(a)}>
                    {a.origin ? `${a.provider} — ${a.origin}` : a.provider}
                  </option>
                ))}
              </select>
            </label>

            {origin !== null && (
              <label className="flex flex-col gap-1">
                <span className="text-nkz-text-muted text-xs">{t('llm.apiBase')}</span>
                <input
                  className={inputClass}
                  type="text"
                  value={apiBase}
                  placeholder={origin}
                  onChange={(ev) => {
                    setApiBase(ev.target.value);
                    setApiKey('');
                  }}
                />
                <span className="text-nkz-text-muted text-xs">
                  {t('llm.apiBaseHint', { origin })}
                </span>
              </label>
            )}

            <label className="flex flex-col gap-1">
              <span className="text-nkz-text-muted text-xs">{t('llm.model')}</span>
              <input
                className={inputClass}
                type="text"
                value={model}
                onChange={(ev) => setModel(ev.target.value)}
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-nkz-text-muted text-xs">{t('llm.apiKey')}</span>
              <input
                className={inputClass}
                type="password"
                autoComplete="new-password"
                value={apiKey}
                placeholder={
                  cfg.configured && !keyRequired && cfg.key_last4 ? `…${cfg.key_last4}` : ''
                }
                onChange={(ev) => setApiKey(ev.target.value)}
              />
              <span className="text-nkz-text-muted text-xs">
                {keyRequired ? t('llm.keyRequired') : t('llm.keyKeep')}
              </span>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-nkz-text-muted text-xs">
                {t('llm.budget', { ceiling: cfg.ceiling })}
              </span>
              <input
                className={inputClass}
                type="number"
                min={1}
                max={cfg.ceiling}
                value={budget}
                onChange={(ev) => setBudget(ev.target.value)}
              />
            </label>

            <div className="flex items-center gap-4 flex-wrap">
              <button
                type="submit"
                className="bg-nkz-accent-base text-nkz-text-on-accent rounded-nkz-md px-4 py-2 text-nkz-sm font-medium"
              >
                {t('llm.save')}
              </button>
              {cfg.configured && (
                <>
                  <button
                    type="button"
                    className="text-nkz-accent-base underline text-nkz-sm"
                    onClick={test}
                  >
                    {t('llm.test')}
                  </button>
                  <button
                    type="button"
                    className="text-nkz-danger text-nkz-sm underline"
                    onClick={remove}
                  >
                    {t('llm.remove')}
                  </button>
                </>
              )}
            </div>
          </form>

          {cfg.configured && cfg.key_last4 && (
            <p className="text-nkz-text-muted text-nkz-sm">
              {t('llm.savedKey', { last4: cfg.key_last4 })}
            </p>
          )}
          {saved && <p className="text-nkz-text-secondary">{t('llm.saved')}</p>}
          {testResult?.ok && <p className="text-nkz-text-secondary">{t('llm.testOk')}</p>}
          {testResult && !testResult.ok && (
            <div className="flex flex-col gap-1">
              <p className="text-nkz-danger">
                {t('llm.testFailed')}{' '}
                {t(
                  testResult.error && TEST_ERRORS.includes(testResult.error)
                    ? `llm.testErrors.${testResult.error}`
                    : 'llm.testErrors.generic',
                )}
              </p>
              {testResult.detail && (
                <p className="text-nkz-text-muted text-nkz-sm break-all">
                  {t('llm.testDetail')} {testResult.detail}
                </p>
              )}
            </div>
          )}
          {errorCode && <p className="text-nkz-danger">{errorText(errorCode)}</p>}
        </>
      )}

      <UsagePanel />
    </div>
  );
}
