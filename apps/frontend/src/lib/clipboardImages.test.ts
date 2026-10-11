import { afterEach, describe, expect, it, vi } from 'vitest';

import { isSafeUploadPath } from './attachmentPath';
import { imagesFromClipboard } from './clipboardImages';

function fileItem(file: File | null) {
  return { kind: 'file', getAsFile: () => file };
}

function clipboard(items: unknown[] | undefined, files: File[] = []): DataTransfer {
  return { items, files } as unknown as DataTransfer;
}

afterEach(() => vi.unstubAllGlobals());

describe('imagesFromClipboard', () => {
  it('reads each image once when items and files represent the same clipboard contents', async () => {
    const pixels = new Uint8Array([0, 255, 128, 10]);
    const first = new File([pixels], 'screenshot.PNG', {
      type: 'image/png',
      lastModified: 1234,
    });
    const second = new File(['another image'], 'photo.jpg', { type: 'image/jpeg' });
    const images = imagesFromClipboard(
      clipboard([fileItem(first), fileItem(second)], [first, second]),
    );

    expect(images).toHaveLength(2);
    expect(images[0].name).toMatch(/^pasted-image-.+\.PNG$/);
    expect(images[0].type).toBe(first.type);
    expect(images[0].lastModified).toBe(first.lastModified);
    expect(new Uint8Array(await images[0].arrayBuffer())).toEqual(pixels);
    expect(images[1].type).toBe(second.type);
    expect(await images[1].text()).toBe('another image');
  });

  it.each([
    ['unavailable items', undefined],
    ['empty items', []],
    ['an item without a readable file', [fileItem(null)]],
  ])('uses the file list with %s', (_description, items) => {
    const image = new File(['pixels'], 'image.png', { type: 'image/png' });
    const text = new File(['notes'], 'notes.txt', { type: 'text/plain' });

    const images = imagesFromClipboard(clipboard(items, [text, image]));

    expect(images).toHaveLength(1);
    expect(images[0].type).toBe('image/png');
    expect(images[0].size).toBe(image.size);
  });

  it.each(['items', 'files'])(
    'keeps both images when clipboard %s exposes an incomplete view',
    async (incomplete) => {
      const first = new File(['first image'], 'image.png', { type: 'image/png' });
      const second = new File(['second image'], 'image.png', { type: 'image/png' });
      const images = imagesFromClipboard(
        clipboard(
          [fileItem(first), fileItem(incomplete === 'items' ? null : second)],
          incomplete === 'files' ? [first] : [first, second],
        ),
      );

      expect(await Promise.all(images.map((image) => image.text()))).toEqual([
        'first image',
        'second image',
      ]);
      expect(new Set(images.map((image) => image.name)).size).toBe(2);
    },
  );

  it('ignores text, HTML image markup, and non-image files without reading them', () => {
    const readString = vi.fn();
    const getData = vi.fn(() => '<img src="data:image/png;base64,eA==">');
    const text = new File(['notes'], 'notes.txt', { type: 'text/plain' });
    const transfer = {
      ...clipboard(
        [
          { kind: 'string', type: 'text/html', getAsString: readString },
          { kind: 'string', type: 'text/plain', getAsString: readString },
          fileItem(text),
        ],
        [text],
      ),
      getData,
    } as DataTransfer;

    expect(imagesFromClipboard(transfer)).toEqual([]);
    expect(readString).not.toHaveBeenCalled();
    expect(getData).not.toHaveBeenCalled();
  });

  it('gives repeated pastes distinct safe upload paths across invocations', () => {
    const source = new File(['pixels'], '../image.png', { type: 'image/png' });
    const transfer = clipboard([fileItem(source)], [source]);
    const names = Array.from({ length: 3 }, () => imagesFromClipboard(transfer)[0].name);

    expect(new Set(names).size).toBe(names.length);
    for (const name of names) {
      expect(isSafeUploadPath(name, 512)).toBe(true);
      expect(name).toMatch(/^pasted-image-.+\.png$/);
    }
    expect(source.name).toBe('../image.png');
  });

  it.each([
    ['image/jpeg', '.jpg'],
    ['image/svg+xml', '.svg'],
  ])('gives a nameless %s image the appropriate extension', (type, extension) => {
    const source = new File(['image contents'], '', { type });

    const [image] = imagesFromClipboard(clipboard([fileItem(source)]));

    expect(image.name.endsWith(extension)).toBe(true);
    expect(image.type).toBe(type);
  });

  it('still creates distinct safe image paths without crypto.randomUUID', () => {
    vi.stubGlobal('crypto', {});
    const source = new File(['pixels'], 'image.png', { type: 'image/png' });
    const transfer = clipboard([fileItem(source)]);
    const names = [imagesFromClipboard(transfer)[0].name, imagesFromClipboard(transfer)[0].name];

    expect(new Set(names).size).toBe(2);
    for (const name of names) expect(isSafeUploadPath(name, 512)).toBe(true);
  });
});
