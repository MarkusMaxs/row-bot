import type {
  AttentionProblem,
  AttentionSnapshot,
  ConversationView,
  PendingApproval,
  PendingApprovalPage,
} from '../../api/types';
import { interruptedWork } from './DelegatedActivity';
import { useSharedRead } from './pending-approvals';

const READ_EVERY_MS = 60_000;

/**
 * What needs the person (problems, each with its fix) and an update, one
 * read shared by the sidebar's indicator and Home's Needs you.
 */
export function useAttention(
  load?: (signal?: AbortSignal) => Promise<AttentionSnapshot>,
) {
  return useSharedRead(load, READ_EVERY_MS, ['monitor']);
}

/** A conversation that waits on the person: an approval, a goal's question or stopped agent work. */
export function waitingOnYou(row: ConversationView) {
  return (
    row.activity_state === 'attention' ||
    Boolean(
      row.generation_state?.some(
        (state) => state.status === 'waiting_approval',
      ),
    )
  );
}

/**
 * Conversations waiting on the person that no listed approval already
 * covers: interrupted agent work, a goal's question, or an approval the list
 * doesn't hold yet.
 */
export function conversationsWaiting(
  rows: readonly ConversationView[],
  approvals: readonly PendingApproval[],
): ConversationView[] {
  const listed = new Set(approvals.map((item) => item.conversation_id));
  return rows.filter(
    (row) =>
      waitingOnYou(row) &&
      (Boolean(interruptedWork(row)) || !listed.has(row.id)),
  );
}

/**
 * How many things need the person. Home's Needs you and the sidebar show this
 * one number for the same items (B383): waiting approvals, problems and
 * conversations waiting on them. The optional setup reminder is not a need.
 */
export function needsCount({
  approvals,
  problems,
  conversations,
}: {
  approvals: PendingApprovalPage | null;
  problems: readonly AttentionProblem[];
  conversations: readonly ConversationView[];
}) {
  return (
    (approvals?.total ?? 0) +
    problems.length +
    conversationsWaiting(conversations, approvals?.items ?? []).length
  );
}
