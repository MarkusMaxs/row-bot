import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import type { ClientController } from '../../api/controller';
import type { ClientPlatform } from '../../platform';
import { RuntimeContext } from '../../runtime';
import ViewOnlyNotice from './ViewOnlyNotice';

function renderWith(mayAct: boolean | undefined, compact = false) {
  const state = {
    handshake: mayAct === undefined ? null : { may_act: mayAct },
  };
  const controller = {
    subscribe: () => () => {},
    getSnapshot: () => state,
  } as unknown as ClientController;
  return render(
    <RuntimeContext.Provider
      value={{ controller, platform: {} as ClientPlatform }}
    >
      <ViewOnlyNotice compact={compact} />
    </RuntimeContext.Provider>,
  );
}

it('says a tab on this computer that no person signed in is view only, and how to sign it in', () => {
  renderWith(false);
  const notice = screen.getByRole('status');
  expect(notice).toHaveTextContent('View only on this computer');
  expect(notice).toHaveAccessibleName(
    /open Row-Bot from its window, or choose Open in Browser from its tray icon/,
  );
});

it('the collapsed rail shows only its icon, with the same words as its name', () => {
  renderWith(false, true);
  const notice = screen.getByRole('status');
  expect(notice).not.toHaveTextContent('View only on this computer');
  expect(notice).toHaveAccessibleName(/^View only\./);
});

it('is absent wherever the person may act, and before the first handshake', () => {
  const { container, rerender } = renderWith(true);
  expect(container).toBeEmptyDOMElement();
  rerender(<></>);
  expect(renderWith(undefined).container).toBeEmptyDOMElement();
});
