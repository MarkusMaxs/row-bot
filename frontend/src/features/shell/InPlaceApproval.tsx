import { useEffect, useRef, useState, type MouseEvent } from 'react';
import { Link } from 'react-router-dom';
import { ArrowUpRight, Check, X } from 'lucide-react';
import type { PendingApproval } from '../../api/types';
import { clientError } from '../../api/errors';
import { useRuntime } from '../../runtime';
import { useOverlay } from '../../ui/overlays';
import { Button, Hint, IconButton, InlineEmpty } from '../../ui/primitives';
import ApprovalCard from './ApprovalCard';
import { unannounced, usePendingApprovals } from './pending-approvals';
import { WaitingSince } from './waiting-since';

export { WaitingSince, waitingSince } from './waiting-since';

/** A request that is no longer waiting: someone answered it elsewhere. */
const ANSWERED = new Set([
  'approval_expired',
  'approval_already_resolved',
  'not_found',
]);

const SOURCE_WORDS: Record<PendingApproval['source'], string> = {
  workflow: 'Workflow',
  conversation: 'Conversation',
  agent: 'Agent',
};

/** "‘Daily News’ needs your approval", or "has a question" when Row-Bot asks one (N4). */
export function waitingWords(item: Pick<PendingApproval, 'title' | 'kind'>) {
  return item.kind === 'question'
    ? `${item.title} has a question`
    : `${item.title} needs your approval`;
}

/**
 * Answer for Row-Bot's question where it waits in a list (N4): the question
 * card, in a dialog, with Answer and Skip. Never Approve / Deny.
 */
function QuestionDecision({
  approvalId,
  subject,
  onResolved,
}: {
  approvalId: string;
  subject: string;
  onResolved?: () => void;
}) {
  const overlay = useOverlay();
  return (
    <Button
      variant="primary"
      className="small"
      aria-label={`Answer: ${subject}`}
      onClick={(event) =>
        overlay.open({
          title: 'Row-Bot has a question',
          description: subject,
          returnFocusTo: event.currentTarget,
          content: (
            <ApprovalCard
              id={approvalId}
              notice
              onResolved={() => {
                onResolved?.();
                overlay.close();
              }}
            />
          ),
        })
      }
    >
      Answer
    </Button>
  );
}

/**
 * Approve / Deny for one pending approval, in place (B255; Phase 18 puts it
 * on Overview). It sends the same reviewed command as the approval card: a
 * fresh view binds this session to the exact stored action, then
 * `approval.resolve` answers it.
 */
export function ApprovalDecision({
  approvalId,
  subject,
  onResolved,
  kind = 'approval',
}: {
  approvalId: string;
  /** What is being approved, for the buttons' group name. */
  subject: string;
  /** After it was answered (here or, as it turned out, elsewhere). */
  onResolved?: () => void;
  /** Row-Bot's question is answered on its card, never approved (N4). */
  kind?: PendingApproval['kind'];
}) {
  if (kind === 'question')
    return (
      <QuestionDecision
        approvalId={approvalId}
        subject={subject}
        onResolved={onResolved}
      />
    );
  return (
    <ApprovalButtons
      approvalId={approvalId}
      subject={subject}
      onResolved={onResolved}
    />
  );
}

function ApprovalButtons({
  approvalId,
  subject,
  onResolved,
}: {
  approvalId: string;
  subject: string;
  onResolved?: () => void;
}) {
  const { controller } = useRuntime();
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState('');
  const [error, setError] = useState('');
  async function decide(decision: 'approve' | 'reject') {
    if (busy || done) return;
    setBusy(true);
    setError('');
    try {
      const view = await controller.approval(approvalId);
      await controller.intent(
        approvalId,
        'approval.resolve',
        { decision, nonce: view.nonce },
        view.revision,
      );
      setDone(decision === 'approve' ? 'Approved' : 'Denied');
      onResolved?.();
    } catch (cause) {
      const failure = clientError(cause);
      if (ANSWERED.has(failure.code)) {
        setDone('Already answered');
        onResolved?.();
      } else setError(failure.message);
    } finally {
      setBusy(false);
    }
  }
  if (done)
    return (
      <p role="status" className="approval-decision-done">
        {done}
      </p>
    );
  return (
    <div
      className="approval-decision"
      role="group"
      aria-label={`Answer: ${subject}`}
    >
      <IconButton
        label="Deny"
        size="sm"
        disabled={busy}
        onClick={() => void decide('reject')}
      >
        <X size={16} aria-hidden />
      </IconButton>
      <IconButton
        label="Approve"
        size="sm"
        variant="primary"
        disabled={busy}
        onClick={() => void decide('approve')}
      >
        <Check size={16} aria-hidden />
      </IconButton>
      {error && (
        <p role="alert" className="approval-decision-error">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * Every waiting approval with Approve / Deny in place, what it is, since
 * when it waits, and a link to where it was asked. Row-Bot's questions have
 * Answer instead (N4).
 */
export function PendingApprovalList({
  items,
  onChanged,
  onNavigate,
}: {
  items: readonly PendingApproval[];
  /** After a decision: the list re-reads. */
  onChanged?: () => void;
  /** Opening where one was asked (a popover, drawer or dialog closes). */
  onNavigate?: (to: string) => (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  if (!items.length)
    return <InlineEmpty>Nothing is waiting for your approval.</InlineEmpty>;
  return (
    <ul
      className="pending-approvals"
      aria-label={
        items.some((item) => item.kind === 'question')
          ? 'Waiting for you'
          : 'Waiting for your approval'
      }
    >
      {items.map((item) => {
        const to = item.task_id
          ? `/?tab=workflows&workflow=${encodeURIComponent(item.task_id)}`
          : item.conversation_id
            ? `/conversations/${encodeURIComponent(item.conversation_id)}`
            : '';
        return (
          <li
            key={item.id}
            className="pending-approval"
            aria-label={waitingWords(item)}
          >
            <div className="pending-approval-text">
              <strong>{item.title}</strong>
              {item.what && (
                <span className="pending-approval-what">{item.what}</span>
              )}
              <small>
                {SOURCE_WORDS[item.source]} ·{' '}
                <WaitingSince value={item.requested_at} />
              </small>
            </div>
            <div className="pending-approval-actions">
              {to && (
                <Hint
                  label={
                    item.task_id ? 'Open the workflow' : 'Open the conversation'
                  }
                >
                  <Link
                    className="button ghost icon-button icon-action icon-action-sm"
                    to={to}
                    aria-label={
                      item.task_id
                        ? 'Open the workflow'
                        : 'Open the conversation'
                    }
                    onClick={onNavigate?.(to)}
                  >
                    <ArrowUpRight size={16} aria-hidden />
                  </Link>
                </Hint>
              )}
              <ApprovalDecision
                approvalId={item.id}
                subject={item.title}
                kind={item.kind}
                onResolved={onChanged}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** The list as a dialog's content, following the shared read. */
function PendingApprovalsReview() {
  const { controller } = useRuntime();
  const overlay = useOverlay();
  const { page, refresh } = usePendingApprovals(controller.pendingApprovals);
  return (
    <PendingApprovalList
      items={page?.items ?? []}
      onChanged={refresh}
      onNavigate={() => () => overlay.close()}
    />
  );
}

/**
 * A floating notice when a new approval arrives, once per approval on this
 * device: "‘Daily News’ needs your approval · Review" opens the list. An
 * approval in the conversation on screen is not announced: its card is there.
 */
export function useApprovalNotices(openConversationId: string | null): void {
  const { controller } = useRuntime();
  const overlay = useOverlay();
  const latest = useRef({ overlay, openConversationId });
  useEffect(() => {
    latest.current = { overlay, openConversationId };
  });
  const { page } = usePendingApprovals(controller.pendingApprovals);
  useEffect(() => {
    if (!page) return;
    const { overlay, openConversationId } = latest.current;
    const fresh = unannounced(page.items).filter(
      (item) =>
        !openConversationId || item.conversation_id !== openConversationId,
    );
    if (!fresh.length) return;
    overlay.notify(
      fresh.length === 1
        ? waitingWords({ ...fresh[0], title: `‘${fresh[0].title}’` })
        : fresh.every((item) => item.kind === 'question')
          ? `${fresh.length} questions are waiting for you`
          : `${fresh.length} approvals are waiting for you`,
      'warning',
      {
        label: 'Review',
        onAction: () =>
          latest.current.overlay.open({
            title: 'Waiting for your approval',
            description: 'Approve or deny here, or open where it was asked.',
            content: <PendingApprovalsReview />,
          }),
      },
    );
  }, [page]);
}
