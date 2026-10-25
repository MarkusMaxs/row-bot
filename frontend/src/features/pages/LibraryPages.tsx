import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { LayoutTemplate, RefreshCw } from 'lucide-react';
import { clientError } from '../../api/errors';
import type { PageView } from '../../api/types';
import { useRuntime } from '../../runtime';
import {
  Button,
  EmptyState,
  ErrorState,
  IconButton,
  Skeleton,
} from '../../ui/primitives';
import { absoluteTime } from '../../ui/format';
import { shortTime } from '../shell/conversation-groups';
import { requestPagePanel } from './open-page';

/**
 * Library › Pages (N5): every page Row-Bot made, most recently changed
 * first, each with its chat. Opening one goes to its chat with the page
 * beside it.
 */
export default function LibraryPages() {
  const { controller } = useRuntime();
  const [items, setItems] = useState<PageView[] | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const abort = new AbortController();
    setError('');
    controller.pages(undefined, abort.signal).then(
      (page) => {
        if (!abort.signal.aborted) setItems(page.items);
      },
      (cause) => {
        if (!abort.signal.aborted) setError(clientError(cause).message);
      },
    );
    return () => abort.abort();
  }, [controller, attempt]);

  const now = new Date();
  return (
    <section className="page-library" aria-label="Pages">
      <div className="page-library-bar">
        <p className="library-summary">
          {items
            ? `${items.length} ${items.length === 1 ? 'page' : 'pages'} · recently changed first`
            : ''}
        </p>
        <IconButton
          label="Refresh pages"
          onClick={() => setAttempt((n) => n + 1)}
        >
          <RefreshCw size={16} aria-hidden />
        </IconButton>
      </div>
      {error ? (
        <ErrorState
          title="Pages didn't load"
          action={
            <Button onClick={() => setAttempt((n) => n + 1)}>Try again</Button>
          }
        >
          {error}
        </ErrorState>
      ) : !items ? (
        <Skeleton label="Loading pages" />
      ) : items.length === 0 ? (
        <EmptyState title="No pages yet">
          Ask Row-Bot for a plan you can approve step by step, a checklist or a
          dashboard. Pages it makes show here, with their chats.
        </EmptyState>
      ) : (
        <ul className="page-library-list">
          {items.map((item) => (
            <li key={item.page_id} className="page-library-row">
              <Link
                className="page-library-link"
                to={`/conversations/${encodeURIComponent(item.conversation_id)}`}
                onClick={() =>
                  requestPagePanel({
                    conversationId: item.conversation_id,
                    pageId: item.page_id,
                    title: item.title,
                  })
                }
              >
                <LayoutTemplate
                  className="page-library-icon"
                  size={16}
                  aria-hidden
                />
                <span className="page-library-text">
                  <span className="page-library-title">{item.title}</span>
                  <span className="page-library-chat">
                    {item.conversation_title
                      ? `In ${item.conversation_title}`
                      : 'In a chat'}
                  </span>
                </span>
                {item.pending > 0 && (
                  <span className="page-library-pending">
                    {item.pending} not sent
                  </span>
                )}
                <time
                  className="library-row-date"
                  dateTime={item.updated_at}
                  title={absoluteTime(item.updated_at)}
                >
                  {shortTime(item.updated_at, now)}
                </time>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
