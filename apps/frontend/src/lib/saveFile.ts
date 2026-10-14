/**
 * Hand a file to the browser's own download.
 *
 * The link is attached while it is clicked, and its object URL released only
 * once the browser has certainly taken the bytes: revoking it straight after
 * the click can fail the download in Firefox and Safari.
 */
export function saveFile(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
