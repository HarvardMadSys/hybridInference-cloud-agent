/** Read clipboard files synchronously while the paste event still owns them. */
export function imagesFromClipboard(clipboard: DataTransfer): File[] {
  const items = Array.from(clipboard.items ?? [])
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null && file.type.startsWith('image/'));
  const files = Array.from(clipboard.files ?? []).filter((file) => file.type.startsWith('image/'));
  // These are two views of the same clipboard. Keep the more complete view
  // when some items are unreadable, without uploading their files twice.
  const images = files.length >= items.length ? files : items;

  return images.map((file) => {
    const subtype = file.type.slice('image/'.length);
    const extension =
      file.name.match(/\.[a-z0-9]+$/i)?.[0] ??
      (subtype === 'jpeg'
        ? '.jpg'
        : subtype === 'svg+xml'
          ? '.svg'
          : /^[a-z0-9]+$/i.test(subtype)
            ? `.${subtype}`
            : '');
    // Clipboard screenshots usually all arrive as "image.png". Upload paths
    // must stay unique across messages and remounts in the same conversation.
    const id =
      globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    return new File([file], `pasted-image-${id}${extension}`, {
      type: file.type,
      lastModified: file.lastModified,
    });
  });
}
