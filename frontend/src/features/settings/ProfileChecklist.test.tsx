import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { expect, it, vi } from 'vitest';
import { ProfileChecklist, type ChecklistOption } from './ProfileChecklist';

const tools: ChecklistOption[] = [
  { id: 'calculator', label: 'Calculator', detail: 'Built in' },
  { id: 'web_search', label: 'Web search', detail: 'Built in' },
];

function Picker({
  initial,
  search,
}: {
  initial: string[];
  search: (query: string) => Promise<ChecklistOption[]>;
}) {
  const [chosen, setChosen] = useState(initial);
  return (
    <ProfileChecklist
      label="Tools"
      selected={chosen}
      onChange={setChosen}
      search={search}
      empty="None chosen: it uses every tool turned on in Settings."
    />
  );
}

it('picks tools by name from a searchable list instead of typed IDs (F14)', async () => {
  const search = vi.fn(async (query: string) =>
    tools.filter((tool) =>
      tool.label.toLowerCase().includes(query.toLowerCase()),
    ),
  );
  render(<Picker initial={[]} search={search} />);
  expect(
    screen.getByText('None chosen: it uses every tool turned on in Settings.'),
  ).toBeVisible();
  fireEvent.click(await screen.findByRole('checkbox', { name: /Calculator/ }));
  expect(screen.getByRole('list', { name: 'Chosen tools' })).toHaveTextContent(
    'Calculator',
  );

  fireEvent.change(screen.getByRole('searchbox', { name: 'Find tools' }), {
    target: { value: 'web' },
  });
  await waitFor(() =>
    expect(search).toHaveBeenLastCalledWith('web', expect.anything()),
  );
  expect(
    await screen.findByRole('checkbox', { name: /Web search/ }),
  ).not.toBeChecked();
  expect(screen.queryByRole('checkbox', { name: /Calculator/ })).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: 'Take out Calculator' }));
  expect(screen.queryByRole('list', { name: 'Chosen tools' })).toBeNull();
});

it('keeps a chosen ID the list no longer holds, by its name', async () => {
  render(<Picker initial={['retired_tool']} search={async () => tools} />);
  await screen.findByRole('checkbox', { name: /Calculator/ });
  expect(screen.getByRole('list', { name: 'Chosen tools' })).toHaveTextContent(
    'Retired tool',
  );
});

it('says when the list cannot be read', async () => {
  render(
    <Picker
      initial={[]}
      search={async () => {
        throw new Error('offline');
      }}
    />,
  );
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'The tools could not be listed. Try again.',
  );
});
