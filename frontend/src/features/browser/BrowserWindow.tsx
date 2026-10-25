import { useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { useClientSelector, useRuntime } from '../../runtime';
import { EmptyState } from '../../ui/primitives';
import BrowserPane from './BrowserPane';

/**
 * The Browser pane popped out to its own window (a second desktop window, or
 * a new tab in a browser): only this conversation's browser, full size.
 */
export default function BrowserWindow() {
  const { conversationId = '' } = useParams();
  const { controller } = useRuntime();
  const ready = useClientSelector((value) => value.status === 'ready');
  const title = useClientSelector((value) =>
    value.conversation?.id === conversationId ? value.conversation.title : '',
  );
  useEffect(() => {
    // The conversation's turn (for Stop) follows this window too.
    if (ready && conversationId)
      void controller.selectConversation(conversationId);
  }, [controller, conversationId, ready]);
  useEffect(() => {
    document.title = title ? `Browser · ${title}` : 'Browser';
  }, [title]);
  if (!conversationId)
    return (
      <main className="browser-window">
        <EmptyState title="No conversation">
          Open the browser from a conversation.
        </EmptyState>
      </main>
    );
  return (
    <main className="browser-window">
      <BrowserPane conversationId={conversationId} mode="window" />
    </main>
  );
}
