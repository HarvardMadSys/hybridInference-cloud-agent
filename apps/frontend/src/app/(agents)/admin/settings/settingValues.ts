import type { AdminSettingDefinition, AdminSettingValue } from '@/lib/api/adminSettings';

export const ARCHIVE_BUCKET = 'sandbox_archive.bucket';
export const ARCHIVE_PREFIX = 'sandbox_archive.prefix';

type ParsedValue =
  | { value: AdminSettingValue; additionalChanges?: Record<string, AdminSettingValue> }
  | { error: string };

function parseArchiveBucket(draft: string): ParsedValue {
  if (!draft) return { value: '' };
  const uri = draft.startsWith('s3://');
  // Keep raw components: URL parsers can silently normalize unsafe path segments.
  const components = uri ? /^s3:\/\/([^/]+)(?:\/(.*))?$/.exec(draft) : null;
  const bucket = uri ? (components?.[1] ?? '') : draft;
  if (
    /\s/.test(draft) ||
    !/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(bucket) ||
    /^\d{1,3}(?:\.\d{1,3}){3}$/.test(bucket) ||
    /\.\.|\.-|-\./.test(bucket) ||
    /^(?:xn--|sthree-|amzn-s3-demo-)/.test(bucket) ||
    /(?:-s3alias|--ol-s3|\.mrap|--x-s3|--table-s3)$/.test(bucket)
  ) {
    return { error: 'Enter a valid S3 bucket name or an S3 URI such as s3://bucket/prefix.' };
  }
  if (!uri) return { value: bucket };
  const path = components?.[2] ?? '';
  const prefix = path.endsWith('/') ? path.slice(0, -1) : path;
  if (
    path.startsWith('/') ||
    path.includes('//') ||
    new TextEncoder().encode(prefix).length > 512 ||
    (prefix &&
      (!/^[a-zA-Z0-9_./-]+$/.test(prefix) ||
        prefix.split('/').some((part) => !part || part === '.' || part === '..')))
  ) {
    return {
      error:
        'Enter an S3 URI with a prefix of at most 512 bytes using letters, numbers, underscores, dots, hyphens, and single slashes. Do not use . or .. path segments, query strings, or fragments.',
    };
  }
  return { value: bucket, additionalChanges: { [ARCHIVE_PREFIX]: prefix } };
}

export function displayValue(value: AdminSettingValue): string {
  return typeof value === 'boolean' ? (value ? 'Enabled' : 'Disabled') : String(value);
}

export function parseValue(definition: AdminSettingDefinition, draft: string): ParsedValue {
  if (definition.kind === 'boolean') return { value: draft === 'true' };
  if (definition.kind === 'string') {
    if (definition.key === ARCHIVE_BUCKET) return parseArchiveBucket(draft);
    if (definition.choices?.length && !definition.choices.includes(draft)) {
      return { error: 'Choose one of the available values.' };
    }
    if (
      draft &&
      definition.minimum_bytes &&
      new TextEncoder().encode(draft).length < definition.minimum_bytes
    ) {
      return { error: `Enter a value of at least ${definition.minimum_bytes} bytes.` };
    }
    return { value: draft };
  }
  const value = Number(draft);
  if (draft.trim() === '' || !Number.isFinite(value)) return { error: 'Enter a number.' };
  if (definition.kind === 'integer' && !Number.isInteger(value)) {
    return { error: 'Enter a whole number.' };
  }
  if (definition.minimum !== undefined && value < definition.minimum) {
    return { error: `Enter a value of at least ${definition.minimum}.` };
  }
  if (definition.maximum !== undefined && value > definition.maximum) {
    return { error: `Enter a value no greater than ${definition.maximum}.` };
  }
  return { value };
}
