/** Invalidate model choices in this tab and other tabs, without sharing keys or model data. */
export const MODEL_CONNECTIONS_CHANGED = 'agents.model-connections.changed';

export function announceModelConnectionsChanged() {
  window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED));
  try {
    localStorage.setItem(MODEL_CONNECTIONS_CHANGED, crypto.randomUUID());
  } catch {
    // Same-tab updates still work when browser storage is unavailable.
  }
}
