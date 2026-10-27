type ClientLifecycle = {
  setOnline(online: boolean): void;
  dispose(): void;
  hasUnsavedDraft?(): boolean;
};
type PageLifecycle = {
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
  navigator: { readonly onLine: boolean };
  location: { readonly hostname: string };
};

/** Row-Bot opened on this computer: reaching it never needs the network. */
function onThisComputer(hostname: string) {
  const host = hostname.replace(/^\[|\]$/g, '');
  return (
    host === 'localhost' ||
    host.endsWith('.localhost') ||
    host === '::1' ||
    host.startsWith('127.')
  );
}

/** Suspend cached documents; returning pages reauthenticate without replaying work. */
export function bindPageLifecycle(
  client: ClientLifecycle,
  page: PageLifecycle = window,
): () => void {
  let cached = false;
  // A Wi-Fi drop makes the browser report offline, but a page from this
  // computer still reaches Row-Bot: it carries on instead of disconnecting (#568).
  const local = onThisComputer(page.location.hostname);
  const online = () =>
    client.setOnline(!cached && (local || page.navigator.onLine));
  const hide: EventListener = (event) => {
    if ((event as PageTransitionEvent).persisted) {
      cached = true;
      online();
    } else client.dispose();
  };
  const show: EventListener = (event) => {
    if ((event as PageTransitionEvent).persisted) {
      cached = false;
      online();
    }
  };
  const listeners: [string, EventListener][] = [
    [
      'beforeunload',
      (event) => {
        if (client.hasUnsavedDraft?.()) {
          event.preventDefault();
          (event as BeforeUnloadEvent).returnValue = '';
        }
      },
    ],
    ['online', online],
    ['offline', online],
    ['pagehide', hide],
    ['pageshow', show],
  ];
  for (const [name, listener] of listeners)
    page.addEventListener(name, listener);
  online();
  return () => {
    for (const [name, listener] of listeners)
      page.removeEventListener(name, listener);
  };
}
