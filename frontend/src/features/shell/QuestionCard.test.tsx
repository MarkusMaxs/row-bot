import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import ApprovalCard from './ApprovalCard';
import { TranscriptCards, cardOf } from './TranscriptCards';

const approval = vi.fn();
const intent = vi.fn();
const pendingApprovals = vi.fn();
vi.mock('../../runtime', () => ({
  useRuntime: () => ({ controller: { approval, intent, pendingApprovals } }),
}));
vi.mock('../../ui/overlays', () => ({
  useOverlay: () => ({ open: vi.fn(), notify: vi.fn(), close: vi.fn() }),
}));

const beta = {
  header: 'Beta timing',
  question: 'When should the beta go out?',
  multi_select: false,
  options: [
    {
      label: 'Beta.1 now',
      description: 'Ship the current build to testers today.',
      recommended: true,
    },
    {
      label: 'After the fixes',
      description: 'Wait for the two open fixes, about two days.',
      recommended: false,
    },
  ],
};
const pages = {
  header: 'Pages',
  question: 'Which pages go in the beta?',
  multi_select: true,
  options: [
    { label: 'Plans', description: '', recommended: false },
    { label: 'Ledger', description: '', recommended: false },
    { label: 'Docs', description: '', recommended: false },
  ],
};

function view(questions: object[]) {
  return {
    id: 'question-a',
    status: 'pending',
    revision: '0',
    nonce: 'n'.repeat(32),
    action_label: 'Row-Bot has a question',
    reason: 'When should the beta go out?',
    policy_revision: '1',
    questions,
  };
}

beforeEach(() => {
  approval.mockReset();
  intent.mockReset();
  intent.mockResolvedValue({ status: 'accepted' });
});

async function ready(questions: object[] = [beta]) {
  approval.mockResolvedValue(view(questions));
  render(<ApprovalCard id="question-a" />);
  const card = await screen.findByRole('complementary', {
    name: 'Row-Bot has a question',
  });
  await waitFor(() =>
    expect(within(card).getByRole('button', { name: 'Skip' })).toBeEnabled(),
  );
  return card;
}

it('asks in plain words, with the recommended option tagged, and answers with the choice', async () => {
  const card = await ready();
  const group = within(card).getByRole('group', {
    name: 'When should the beta go out?',
  });
  expect(within(card).getByRole('listitem')).toHaveTextContent('Beta timing');
  const now = within(group).getByRole('radio', { name: /Beta\.1 now/ });
  expect(now).toHaveAccessibleDescription(
    'Ship the current build to testers today.',
  );
  expect(within(group).getByText('Recommended')).toBeVisible();
  const answer = within(card).getByRole('button', { name: 'Answer' });
  // One primary action, ready once there is something to answer with.
  expect(answer).toBeDisabled();
  fireEvent.click(now);
  expect(answer).toBeEnabled();
  fireEvent.click(answer);
  await waitFor(() => expect(intent).toHaveBeenCalledOnce());
  expect(intent).toHaveBeenCalledWith(
    'question-a',
    'approval.resolve',
    {
      decision: 'approve',
      nonce: 'n'.repeat(32),
      answers: [{ choices: [0], other: '' }],
    },
    '0',
  );
  expect(await within(card).findByRole('status')).toHaveTextContent(
    'Answered. Row-Bot goes on with your answer.',
  );
});

it('opens a text field for Other and answers with the person’s own words on Enter', async () => {
  const card = await ready();
  fireEvent.click(within(card).getByRole('radio', { name: 'Other…' }));
  const field = within(card).getByRole('textbox', { name: 'Your own answer' });
  await waitFor(() => expect(field).toHaveFocus());
  fireEvent.change(field, { target: { value: 'only plans for now' } });
  fireEvent.keyDown(field, { key: 'Enter' });
  await waitFor(() => expect(intent).toHaveBeenCalledOnce());
  expect(intent.mock.calls[0][2]).toMatchObject({
    decision: 'approve',
    answers: [{ choices: [], other: 'only plans for now' }],
  });
});

it('skips without an answer, so Row-Bot goes on with its recommendation', async () => {
  const card = await ready();
  fireEvent.click(within(card).getByRole('button', { name: 'Skip' }));
  await waitFor(() => expect(intent).toHaveBeenCalledOnce());
  expect(intent.mock.calls[0][2]).toEqual({
    decision: 'reject',
    nonce: 'n'.repeat(32),
  });
  expect(await within(card).findByRole('status')).toHaveTextContent(
    'Skipped. Row-Bot goes on with its recommendation.',
  );
});

it('asks several questions as steps, with checkboxes where several may be picked and Answer at the end', async () => {
  const card = await ready([beta, pages]);
  expect(
    within(card).getByRole('list', { name: 'Question 1 of 2' }),
  ).toBeVisible();
  expect(within(card).queryByRole('button', { name: 'Answer' })).toBeNull();
  fireEvent.click(within(card).getByRole('radio', { name: /After the fixes/ }));
  fireEvent.click(within(card).getByRole('button', { name: 'Next' }));
  const second = within(card).getByRole('group', {
    name: 'Which pages go in the beta?',
  });
  fireEvent.click(within(second).getByRole('checkbox', { name: 'Plans' }));
  fireEvent.click(within(second).getByRole('checkbox', { name: 'Docs' }));
  // Back keeps what was chosen.
  fireEvent.click(within(card).getByRole('button', { name: 'Back' }));
  expect(
    within(card).getByRole('radio', { name: /After the fixes/ }),
  ).toBeChecked();
  fireEvent.click(within(card).getByRole('button', { name: 'Next' }));
  expect(within(card).getByRole('checkbox', { name: 'Docs' })).toBeChecked();
  fireEvent.click(within(card).getByRole('button', { name: 'Answer' }));
  await waitFor(() => expect(intent).toHaveBeenCalledOnce());
  expect(intent.mock.calls[0][2].answers).toEqual([
    { choices: [1], other: '' },
    { choices: [0, 2], other: '' },
  ]);
});

it('picks with 1–4 and answers with Enter from the keyboard', async () => {
  const card = await ready();
  await act(async () => {
    fireEvent.keyDown(document.body, { key: '2' });
  });
  expect(
    within(card).getByRole('radio', { name: /After the fixes/ }),
  ).toBeChecked();
  await act(async () => {
    fireEvent.keyDown(document.body, { key: 'Enter' });
  });
  await waitFor(() => expect(intent).toHaveBeenCalledOnce());
  expect(intent.mock.calls[0][2].answers).toEqual([
    { choices: [1], other: '' },
  ]);
});

it('never approves a question with Ctrl+Enter', async () => {
  await ready();
  await act(async () => {
    fireEvent.keyDown(document.body, { key: 'Enter', ctrlKey: true });
  });
  expect(intent).not.toHaveBeenCalled();
});

it('collapses an answered question to what was chosen, plain and quiet', () => {
  const answered = cardOf(
    {
      kind: 'question_answered',
      outcome: 'answered',
      answers: [{ header: 'Beta timing', answer: 'Beta.1 now' }],
    },
    'call-a',
  );
  const skipped = cardOf(
    { kind: 'question_answered', outcome: 'skipped', answers: [] },
    'call-b',
  );
  const several = cardOf(
    {
      kind: 'question_answered',
      outcome: 'answered',
      answers: [
        { header: 'Beta timing', answer: 'Beta.1 now' },
        { header: 'Pages', answer: '“only plans for now”' },
      ],
    },
    'call-c',
  );
  render(<TranscriptCards cards={[answered!, skipped!, several!]} />);
  expect(
    screen.getByRole('group', { name: 'You chose: Beta.1 now' }),
  ).toHaveTextContent('You chose: Beta.1 now');
  expect(
    screen.getByRole('group', { name: 'You skipped the question' }),
  ).toBeVisible();
  expect(
    screen.getByRole('group', {
      name: 'You chose: Beta timing: Beta.1 now · Pages: “only plans for now”',
    }),
  ).toBeVisible();
});
