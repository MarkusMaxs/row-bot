/**
 * Something in the chat (the live browser step, an approval's picture) asks the
 * workspace to open a conversation's Browser pane. Cards can render outside
 * the workspace's React tree, so this is a small module-level channel.
 */
type Listener = (conversationId: string) => void;

const listeners = new Set<Listener>();

/** True when a workspace is listening (and will open the pane). */
export function requestBrowserPane(conversationId: string): boolean {
  for (const listener of listeners) listener(conversationId);
  return listeners.size > 0;
}

export function onBrowserPaneRequest(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export const BROWSER_PANEL = {
  panel_kind: 'browser.live',
  title: 'Browser',
  required_capabilities: ['browser_navigate'],
} as const;
