type Page = {
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
  fetch(input: string, init?: RequestInit): Promise<Response>;
  document: { querySelector(selector: string): Element | null };
  location: { reload(): void };
};

const ENTRY = /index-[\w-]+\.js/;

/**
 * After an update, a page still running the old build can't load the parts
 * it hasn't opened yet: their files are gone (#568). When one fails to load
 * and Row-Bot now serves a different build, reload to the new one. A load
 * that failed for any other reason shows its error as before.
 */
export function reloadAfterUpdate(page: Page = window): () => void {
  let checking = false;
  const check = async () => {
    if (checking) return;
    checking = true;
    try {
      const running = page.document
        .querySelector('script[type="module"][src*="/assets/index-"]')
        ?.getAttribute('src')
        ?.match(ENTRY)?.[0];
      const response = await page.fetch('/app-v2/', { cache: 'no-store' });
      const served = response.ok
        ? (await response.text()).match(ENTRY)?.[0]
        : undefined;
      if (running && served && served !== running) page.location.reload();
    } catch {
      // Row-Bot isn't reachable now: the part's own error stays on screen.
    } finally {
      checking = false;
    }
  };
  page.addEventListener('vite:preloadError', check);
  return () => page.removeEventListener('vite:preloadError', check);
}
