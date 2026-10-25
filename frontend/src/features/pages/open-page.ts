/**
 * The chat's page card, or Library › Pages, asks the workspace to open a page
 * beside its chat (N5): the side panel on desktop, full screen on a phone.
 * Cards render outside the workspace's React tree, so this is a small
 * module-level channel. A request for a chat that isn't open yet waits until
 * the workspace opens that chat.
 */
export type PageRequest = {
  conversationId: string;
  pageId: string;
  title: string;
};
type Listener = (request: PageRequest) => boolean;

const listeners = new Set<Listener>();
let waiting: PageRequest | null = null;

export const PAGE_PANEL_KIND = 'page.view';

/** True when a workspace showing that chat opened the page. */
export function requestPagePanel(request: PageRequest): boolean {
  for (const listener of listeners) if (listener(request)) return true;
  waiting = request;
  return false;
}

/** The page asked for this chat before it was open, once. */
export function takeWaitingPage(conversationId: string): PageRequest | null {
  if (waiting?.conversationId !== conversationId) return null;
  const found = waiting;
  waiting = null;
  return found;
}

export function onPageRequest(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
