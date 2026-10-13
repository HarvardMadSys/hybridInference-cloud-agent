import type { AgentPluginInputApi } from '@/lib/api/agents';

export const MAX_PLUGINS = 8;
export const MAX_PLUGIN_LENGTH = 128;
export const MAX_MARKETPLACE_LENGTH = 512;

const NPM_PLUGIN_PATTERN =
  /^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*(?:@(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)?$/;

export function marketplacePluginRuntime(runtime: string): boolean {
  return runtime === 'claude-code' || runtime === 'codex';
}

export function normalizeNpmPlugin(value: string): string {
  return value.replace(/^npm:/, '');
}

export function npmPluginIdentity(value: string): string {
  const normalized = normalizeNpmPlugin(value);
  const versionAt = normalized.lastIndexOf('@');
  return versionAt > 0 ? normalized.slice(0, versionAt) : normalized;
}

export function validNpmPlugin(value: string): boolean {
  return value.length <= MAX_PLUGIN_LENGTH && NPM_PLUGIN_PATTERN.test(normalizeNpmPlugin(value));
}

/** The accepted marketplace forms carry no embedded authentication. */
export function validPluginMarketplace(value: string): boolean {
  if (
    value.length < 3 ||
    value.length > MAX_MARKETPLACE_LENGTH ||
    /[\s\u0000-\u001f\u007f]/.test(value)
  )
    return false;
  if (/^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(value)) return true;
  if (!/^https:\/\/[^/\\]/i.test(value) || value.split('/')[2]?.includes('@')) return false;
  try {
    const url = new URL(value);
    return (
      url.protocol === 'https:' &&
      !!url.hostname &&
      (!url.port || url.port === '443') &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash
    );
  } catch {
    return false;
  }
}

export function validPluginName(value: string, runtime = 'claude-code'): boolean {
  const pattern =
    runtime === 'codex'
      ? /^[A-Za-z0-9][A-Za-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9_-]*$/
      : /^[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*$/;
  return value.length <= MAX_PLUGIN_LENGTH && pattern.test(value);
}

export function validPluginForRuntime(runtime: string, selection: AgentPluginInputApi): boolean {
  if (marketplacePluginRuntime(runtime))
    return (
      validPluginName(selection.plugin, runtime) &&
      !!selection.marketplace &&
      validPluginMarketplace(selection.marketplace)
    );
  return (
    ['pi', 'opencode', 'kilo'].includes(runtime) &&
    selection.marketplace === undefined &&
    validNpmPlugin(selection.plugin)
  );
}

/** An older draft has no plugins. Malformed stored values never reach the API. */
export function readDraftPlugins(value: unknown): AgentPluginInputApi[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > MAX_PLUGINS) return null;
  const names = new Set<string>();
  const marketplaceSources = new Map<string, string>();
  for (const item of value) {
    if (
      !item ||
      typeof item !== 'object' ||
      typeof item.plugin !== 'string' ||
      !(item.marketplace === undefined
        ? validNpmPlugin(item.plugin)
        : typeof item.marketplace === 'string' &&
          validPluginName(item.plugin) &&
          validPluginMarketplace(item.marketplace)) ||
      names.has(item.marketplace === undefined ? npmPluginIdentity(item.plugin) : item.plugin)
    )
      return null;
    names.add(item.marketplace === undefined ? npmPluginIdentity(item.plugin) : item.plugin);
    if (typeof item.marketplace === 'string') {
      const name = item.plugin.split('@')[1];
      const existingSource = marketplaceSources.get(name);
      if (existingSource !== undefined && existingSource !== item.marketplace) return null;
      marketplaceSources.set(name, item.marketplace);
    }
  }
  return value;
}
