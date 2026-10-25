import { useEffect, useState } from 'react';
import { LayoutTemplate } from 'lucide-react';
import type { PageView } from '../../api/types';
import { useClientSelector, useRuntime } from '../../runtime';
import { Button } from '../../ui/primitives';
import { requestPagePanel } from './open-page';
import { decisionCount } from './page-words';

/**
 * A page a step made or changed (N5), in the chat: "Launch plan · page" with
 * Open, which shows it beside the chat (full screen on a phone). Its title
 * and the decisions not yet sent are read again by id.
 */
export function PageCard({
  pageId,
  conversationId,
}: {
  pageId: string;
  conversationId: string | null | undefined;
}) {
  const { controller } = useRuntime();
  const [view, setView] = useState<PageView | null>(null);
  const [gone, setGone] = useState(false);
  // Read again as the chat moves on: the agent changed it, or its decisions were sent.
  const revision = useClientSelector((value) =>
    value.projection?.conversation_id === conversationId
      ? value.projection?.checkpoint_revision
      : undefined,
  );

  useEffect(() => {
    const abort = new AbortController();
    controller.page(pageId, abort.signal).then(
      (value) => {
        if (!abort.signal.aborted) setView(value);
      },
      () => {
        if (!abort.signal.aborted) setGone(true);
      },
    );
    return () => abort.abort();
  }, [controller, pageId, revision]);

  const title = view?.title ?? '';
  if (gone)
    return (
      <div
        className="transcript-card"
        data-kind="page"
        data-state="removed"
        role="group"
        aria-label="Page no longer here"
      >
        <LayoutTemplate className="transcript-card-icon" aria-hidden />
        <div className="transcript-card-text">
          <span>This page is no longer here.</span>
        </div>
      </div>
    );
  return (
    <div
      className="transcript-card page-card"
      data-kind="page"
      role="group"
      aria-label={title ? `${title} · page` : 'Page'}
      aria-busy={!view}
    >
      <LayoutTemplate className="transcript-card-icon" aria-hidden />
      <div className="transcript-card-text">
        <span>
          <strong>{title || 'Page'}</strong> · page
        </span>
        {view && view.pending > 0 && (
          <small className="page-card-pending">
            {decisionCount(view.pending)} not sent yet
          </small>
        )}
      </div>
      <div className="transcript-card-actions">
        <Button
          disabled={!view || !conversationId}
          onClick={() =>
            view &&
            conversationId &&
            requestPagePanel({
              conversationId,
              pageId,
              title: view.title,
            })
          }
        >
          Open
        </Button>
      </div>
    </div>
  );
}
