import { HatGlasses } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import type { ConversationView } from '../../api/types';
import { clientError } from '../../api/errors';
import { useRuntime } from '../../runtime';
import { useOverlay } from '../../ui/overlays';
import { Hint, type MenuAction } from '../../ui/primitives';

/**
 * Incognito chats (N11): nothing in one is read for memory, and Row-Bot saves
 * to memory from it only when the person says so. No banner and no setting:
 * an icon where the chat shows, the composer's words, and its ⋯ switch.
 */
export const INCOGNITO_HINT = 'Incognito: nothing here is saved to memory';
export const INCOGNITO_PLACEHOLDER = 'Message Row-Bot (incognito)';

type Switchable = Pick<ConversationView, 'id' | 'revision' | 'incognito'>;

/** The chat's Incognito switch. Turning it on says what stays, with the way to Knowledge. */
export function useIncognitoSwitch() {
  const { controller } = useRuntime();
  const overlay = useOverlay();
  const navigate = useNavigate();
  return async (conversation: Switchable) => {
    const on = !conversation.incognito;
    try {
      await controller.intent(
        conversation.id,
        'conversation.incognito',
        { incognito: on },
        conversation.revision,
      );
    } catch (cause) {
      overlay.notify(clientError(cause).message, 'warning');
      return;
    }
    if (on)
      overlay.notify(
        'Incognito is on: nothing from now is saved to memory. Memories already saved from this chat stay.',
        undefined,
        {
          label: 'Review in Knowledge',
          onAction: () => navigate('/settings/knowledge'),
        },
      );
    else overlay.notify('Incognito is off: memory reads this chat from now.');
  };
}

/** The ⋯ menu's switch: checked while the chat is incognito. */
export function incognitoAction(
  conversation: Switchable,
  turn: (conversation: Switchable) => Promise<void>,
  options: Partial<MenuAction> = {},
): MenuAction {
  return {
    label: 'Incognito',
    description: conversation.incognito
      ? 'On: nothing here is saved to memory'
      : 'Keep this chat out of memory',
    icon: <HatGlasses size={16} />,
    selected: Boolean(conversation.incognito),
    onSelect: () => void turn(conversation),
    ...options,
  };
}

/** The header's mark of an incognito chat, its words one hover or focus away. */
export function IncognitoMark() {
  return (
    <Hint label={INCOGNITO_HINT}>
      <span
        className="conversation-incognito"
        role="img"
        aria-label={INCOGNITO_HINT}
        tabIndex={0}
      >
        <HatGlasses size={15} aria-hidden />
      </span>
    </Hint>
  );
}
