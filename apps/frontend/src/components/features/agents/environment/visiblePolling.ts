/** A single-flight poll that cancels hidden/unmounted reads and resumes on visibility. */
export function pollWhileVisible(read: (signal: AbortSignal) => Promise<number>, initialDelay = 0) {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | null = null;
  let resume = false;

  const schedule = (delay: number) => {
    clearTimeout(timer);
    if (!stopped && !document.hidden) timer = setTimeout(() => void refresh(), delay);
  };
  const refresh = async () => {
    if (stopped || document.hidden || controller) return;
    controller = new AbortController();
    let delay = 5000;
    try {
      delay = await read(controller.signal);
    } catch {
      // The caller displays read errors; a cancellation is intentionally silent.
    } finally {
      controller = null;
      schedule(resume ? 0 : delay);
      resume = false;
    }
  };
  const visibility = () => {
    clearTimeout(timer);
    if (document.hidden) controller?.abort();
    else if (controller) resume = true;
    else void refresh();
  };
  document.addEventListener('visibilitychange', visibility);
  if (initialDelay) schedule(initialDelay);
  else void refresh();
  return () => {
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
    document.removeEventListener('visibilitychange', visibility);
  };
}
