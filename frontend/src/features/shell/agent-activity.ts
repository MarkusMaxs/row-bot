import type { ConversationView } from '../../api/types';

const AGENT_WORK: Record<string, string> = {
  background: 'Background agents working',
  child_running: 'Child agents working',
  approval_wait: 'Agent group needs approval',
  retry: 'Retrying agent work',
  stopping: 'Stopping agent work',
  later_wave_parent: 'Preparing agent work',
};

/**
 * What a conversation's agents are doing, in the same words in the sidebar
 * and on Home; never the phase's own name (B333).
 */
export function agentWorkLabel(
  phase: ConversationView['activity_phase'],
): string {
  return AGENT_WORK[phase ?? ''] ?? 'Agents working';
}
