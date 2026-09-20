// Fallbacks for older control planes; /config advertises the server bounds.
//
// These are enforced twice on purpose. The browser checks them so a user
// learns a file is too large before the bytes travel and before Run is
// pressed; the control plane checks them again because a browser check is a
// convenience, not a boundary.
export const DEFAULT_ATTACHMENT_LIMITS = {
  max_files: 50,
  max_file_bytes: 5 * 1024 * 1024,
  max_total_bytes: 20 * 1024 * 1024,
  max_path_length: 512,
};
export type AttachmentLimits = typeof DEFAULT_ATTACHMENT_LIMITS;

/**
 * Sizes read as "1.8 MB", not "1887436.8 bytes".
 *
 * A whole number keeps its shape: the limit is documented as 5 MB, so the
 * message that quotes it should say 5 MB rather than 5.0 MB.
 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  const megabytes = bytes / (1024 * 1024);
  return `${Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)} MB`;
}
