import { Eye } from 'lucide-react';
import { useClientSelector } from '../../runtime';
import { Hint } from '../../ui/primitives';

const DETAIL =
  'To make changes here, open Row-Bot from its window, or choose Open in Browser from its tray icon. Programs Row-Bot runs on this computer can look, never act as you.';

/**
 * On this computer only a window or browser a person opened may change
 * anything (A0): a program Row-Bot started, or its own browser, can look but
 * never act as the person. Such a tab says so where the sidebar shows what
 * needs the person, and how to sign it in; the rail shows its icon.
 */
export default function ViewOnlyNotice({
  compact = false,
}: {
  compact?: boolean;
}) {
  const viewOnly = useClientSelector(
    (state) => state.handshake?.may_act === false,
  );
  if (!viewOnly) return null;
  return (
    <Hint label={DETAIL}>
      <p
        className={`nav-attention${compact ? ' is-compact button ghost icon-button icon-action icon-action-md' : ''}`}
        data-tone="info"
        role="status"
        tabIndex={0}
        aria-label={`View only. ${DETAIL}`}
      >
        <Eye size={compact ? 18 : 15} aria-hidden />
        {!compact && <span>View only on this computer</span>}
      </p>
    </Hint>
  );
}
