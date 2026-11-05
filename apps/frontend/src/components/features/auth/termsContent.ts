// The deployment's Terms of Service as a reader sees them: which text or link
// to show, a key that changes when the Terms themselves change, and the text
// split into what a reader scans for (the date, numbered sections,
// paragraphs). This only formats: every character the deployment wrote is
// rendered, in order, as text. Nothing is read as HTML or Markdown.
import type { AuthOptions } from '@/lib/api/accounts';

type TermsOptions = Pick<AuthOptions, 'terms_url' | 'terms_text' | 'terms_by_locale'>;

export type ShownTerms =
  /**
   * Text to read here. `locale` is the deployment file it came from, or null
   * for the Admin text, whose language is not declared. `url` is an
   * additional published copy, or empty.
   */
  | { kind: 'text'; text: string; locale: string | null; url: string; key: string }
  /** Only a link to Terms published elsewhere, which this page cannot show. */
  | { kind: 'url'; url: string; key: string }
  /** Nothing published, though sign-up still needs the agreement. */
  | { kind: 'none'; key: string };

/** An absolute http(s) URL, or empty: nothing else becomes a link. */
function httpUrl(value: string | undefined): string {
  const raw = (value ?? '').trim();
  if (!raw) return '';
  try {
    const parsed = new URL(raw);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.toString() : '';
  } catch {
    return '';
  }
}

/**
 * The Terms this deployment published, in the reader's language when it wrote
 * one, else English, else whichever it wrote. Text to read in the page comes
 * before a link elsewhere.
 */
export function shownTerms(options: TermsOptions, locale: string): ShownTerms {
  const files = Object.entries(options.terms_by_locale ?? {})
    .filter((entry): entry is [string, string] => typeof entry[1] === 'string')
    .filter(([, text]) => text.trim() !== '')
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const url = httpUrl(options.terms_url);
  // Every version and the link: a change to any of them is a change to what
  // was agreed to. The reader's language is not part of it.
  const key = JSON.stringify([url, options.terms_text ?? '', files]);
  if (files.length) {
    const byLocale = Object.fromEntries(files);
    const shown = byLocale[locale] ? locale : byLocale['en'] ? 'en' : files[0][0];
    return { kind: 'text', text: byLocale[shown], locale: shown, url, key };
  }
  if ((options.terms_text ?? '').trim())
    return { kind: 'text', text: options.terms_text, locale: null, url, key };
  if (url) return { kind: 'url', url, key };
  return { kind: 'none', key };
}

export interface TermsSection {
  /** The number as written, such as `1.` or `一、`. */
  marker: string;
  /** A short leading title, as written; empty when the section has none. */
  title: string;
  paragraphs: string[];
}

export interface TermsDocumentModel {
  /** Leading "Last updated" lines. */
  dates: string[];
  /** Paragraphs before the first numbered section, or all of an unnumbered text. */
  preamble: string[];
  sections: TermsSection[];
}

const DATE_LINE =
  /^(?:last\s+(?:updated|revised|modified)|updated|revised|effective(?:\s+date)?|effective\s+as\s+of|最后更新|最近更新|更新日期|更新时间|修订日期|生效日期)(?:\s|[:：]|$)/i;
// `1.` `2)` `3、` `一、` `第一条`. A digit straight after the dot is a decimal,
// not a number: "1.5 GB" stays a paragraph.
const SECTION_MARKER =
  /^(\d{1,3}[.)．、](?!\d)|[一二三四五六七八九十百]+[、.．]|第[一二三四五六七八九十百零〇\d]+条)\s*/;
// A title is a short first sentence on the first line: it ends at `.` before
// a space, or at a full-width stop.
const TITLE_END = /^(.{1,60}?(?:\.(?=\s)|。|．))\s*([\s\S]+)$/;
const SHORT_LINE = /^[^\n]{1,60}$/;

function sectionFrom(marker: string, rest: string): TermsSection {
  const sentence = TITLE_END.exec(rest);
  if (sentence) return { marker, title: sentence[1], paragraphs: [sentence[2]] };
  const lineBreak = rest.indexOf('\n');
  const firstLine = lineBreak < 0 ? rest : rest.slice(0, lineBreak).trimEnd();
  if (SHORT_LINE.test(firstLine)) {
    const body = lineBreak < 0 ? '' : rest.slice(lineBreak + 1).trim();
    return { marker, title: firstLine, paragraphs: body ? [body] : [] };
  }
  return { marker, title: '', paragraphs: [rest] };
}

/** Split Terms text into its date, sections and paragraphs, keeping every word. */
export function parseTerms(text: string): TermsDocumentModel {
  const parts = text
    .replace(/\r\n?/g, '\n')
    .split(/\n\s*\n/)
    .map((part) => part.trim())
    .filter(Boolean);
  const model: TermsDocumentModel = { dates: [], preamble: [], sections: [] };
  for (const part of parts) {
    const marker = SECTION_MARKER.exec(part);
    const rest = marker ? part.slice(marker[0].length) : '';
    if (marker && rest) {
      model.sections.push(sectionFrom(marker[1], rest));
    } else if (
      !model.preamble.length &&
      !model.sections.length &&
      model.dates.length < 2 &&
      part.length <= 120 &&
      !part.includes('\n') &&
      DATE_LINE.test(part)
    ) {
      model.dates.push(part);
    } else if (model.sections.length) {
      model.sections[model.sections.length - 1].paragraphs.push(part);
    } else {
      model.preamble.push(part);
    }
  }
  return model;
}
