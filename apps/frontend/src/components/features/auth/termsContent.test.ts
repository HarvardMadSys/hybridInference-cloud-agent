import { describe, expect, it } from 'vitest';
import { parseTerms, shownTerms, type TermsDocumentModel } from './termsContent';

/** Everything a model renders, in order. */
function flat(model: TermsDocumentModel): string {
  return [
    ...model.dates,
    ...model.preamble,
    ...model.sections.flatMap((section) => [section.marker, section.title, ...section.paragraphs]),
  ].join('');
}
const squeeze = (text: string) => text.replace(/\s+/g, '');

describe('parseTerms', () => {
  it('finds the date and numbered sections with their titles', () => {
    const model = parseTerms(
      'Last updated: November 4, 2026.\n\n1. About these Terms. You agree.\n\n2. Accounts. Be accurate.\n\nMore about accounts.',
    );
    expect(model.dates).toEqual(['Last updated: November 4, 2026.']);
    expect(model.preamble).toEqual([]);
    expect(model.sections).toEqual([
      { marker: '1.', title: 'About these Terms.', paragraphs: ['You agree.'] },
      { marker: '2.', title: 'Accounts.', paragraphs: ['Be accurate.', 'More about accounts.'] },
    ]);
  });

  it('reads Chinese numbering and full-width stops', () => {
    const model = parseTerms(
      '最后更新：2026 年 11 月 4 日。\n\n1. 关于本条款。Cloud Agent 是研究性服务。\n\n二、账号。请妥善保管密码。\n\n第三条 联系我们\n邮件联系。',
    );
    expect(model.dates).toEqual(['最后更新：2026 年 11 月 4 日。']);
    expect(model.sections).toEqual([
      { marker: '1.', title: '关于本条款。', paragraphs: ['Cloud Agent 是研究性服务。'] },
      { marker: '二、', title: '账号。', paragraphs: ['请妥善保管密码。'] },
      { marker: '第三条', title: '联系我们', paragraphs: ['邮件联系。'] },
    ]);
  });

  it('keeps a section without a short title as one paragraph under its number', () => {
    const long =
      'You must be told about every change to these terms by email, at least thirty days before it takes effect, without exception.';
    expect(parseTerms(`3) ${long}`).sections).toEqual([
      { marker: '3)', title: '', paragraphs: [long] },
    ]);
    expect(parseTerms('4. Heading only\n\nIts body.').sections).toEqual([
      { marker: '4.', title: 'Heading only', paragraphs: ['Its body.'] },
    ]);
  });

  it('leaves unnumbered text, decimals and a later date line as paragraphs', () => {
    const model = parseTerms(
      'Welcome.\n\nLast updated: yesterday.\n\n1.5 GB is the limit.\n\n2026. A year, not a section.',
    );
    expect(model.dates).toEqual([]);
    expect(model.sections).toEqual([]);
    expect(model.preamble).toEqual([
      'Welcome.',
      'Last updated: yesterday.',
      '1.5 GB is the limit.',
      '2026. A year, not a section.',
    ]);
  });

  it('drops no character of any input, only whitespace between parts', () => {
    const inputs = [
      'Last updated: May 1.\r\n\r\n1. A. b\n\n\n\n2.\n\n3. 中文。正文\n  indented line',
      '<script>alert(1)</script>\n\n1. <b>x</b>. y',
      `1. ${'x'.repeat(200)}`,
      '   ',
      '',
    ];
    for (const input of inputs) expect(squeeze(flat(parseTerms(input)))).toBe(squeeze(input));
  });
});

describe('shownTerms', () => {
  const files = { en: 'English.', 'zh-CN': '中文。' };

  it('picks the reader’s file, then English, then any, then the Admin text, then a link', () => {
    expect(
      shownTerms({ terms_url: '', terms_text: 'x', terms_by_locale: files }, 'zh-CN'),
    ).toMatchObject({ kind: 'text', text: '中文。', locale: 'zh-CN' });
    expect(
      shownTerms({ terms_url: '', terms_text: '', terms_by_locale: { 'zh-CN': '中文。' } }, 'en'),
    ).toMatchObject({ kind: 'text', locale: 'zh-CN' });
    expect(shownTerms({ terms_url: 'https://a.test/t', terms_text: 'Admin.' }, 'en')).toMatchObject(
      { kind: 'text', text: 'Admin.', locale: null, url: 'https://a.test/t' },
    );
    expect(shownTerms({ terms_url: 'https://a.test/t', terms_text: ' ' }, 'en')).toMatchObject({
      kind: 'url',
      url: 'https://a.test/t',
    });
    expect(shownTerms({ terms_url: 'ftp://a.test/t', terms_text: '' }, 'en').kind).toBe('none');
  });

  it('keys the Terms by their content, not by the language read', () => {
    const base = { terms_url: '', terms_text: 'English.', terms_by_locale: files };
    const key = shownTerms(base, 'en').key;
    expect(shownTerms(base, 'zh-CN').key).toBe(key);
    expect(
      shownTerms({ ...base, terms_by_locale: { 'zh-CN': '中文。', en: 'English.' } }, 'en').key,
    ).toBe(key);
    expect(
      shownTerms({ ...base, terms_by_locale: { ...files, 'zh-CN': '新的中文。' } }, 'en').key,
    ).not.toBe(key);
    expect(shownTerms({ ...base, terms_url: 'https://a.test/t' }, 'en').key).not.toBe(key);
  });
});
