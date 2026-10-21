import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import SafeMarkdown from './chat-parity-markdown';

/** Each typeset formula as written, and whether it is display maths (from KaTeX's MathML). */
async function formulas(expected: number) {
  const found = await screen.findAllByRole('math');
  expect(found).toHaveLength(expected);
  return found.map((math) => ({
    tex: math.querySelector('annotation')?.textContent ?? '',
    display: math.getAttribute('display') === 'block',
  }));
}

function show(lines: string[]) {
  return render(<SafeMarkdown text={lines.join('\n')} />);
}

it('typesets \\( … \\) and $…$ inline, and \\[ … \\] and $$ … $$ as display maths', async () => {
  show([
    'Inline \\(a^2 + b^2\\) and $c_1$ in a sentence.',
    '',
    '\\[\\sum_{i=1}^{n} i\\]',
    '',
    '$$E = mc^2$$',
    '',
    'Also $$x^3$$ within a line.',
  ]);
  expect(await formulas(5)).toEqual([
    { tex: 'a^2 + b^2', display: false },
    { tex: 'c_1', display: false },
    { tex: '\\sum_{i=1}^{n} i', display: true },
    { tex: 'E = mc^2', display: true },
    { tex: 'x^3', display: true },
  ]);
  // The words around inline maths stay text.
  expect(screen.getByText(/in a sentence\./)).toBeVisible();
});

it('reads display maths over several lines as one formula', async () => {
  show([
    'The area:',
    '',
    '$$',
    '\\int_0^1 x \\, dx',
    '= \\frac{1}{2}',
    '$$',
    '',
    '\\[',
    'a \\le b',
    '\\]',
  ]);
  expect(await formulas(2)).toEqual([
    { tex: '\\int_0^1 x \\, dx\n= \\frac{1}{2}', display: true },
    { tex: 'a \\le b', display: true },
  ]);
  expect(screen.getByText('The area:')).toBeVisible();
});

it.each([
  'It costs $5 and $10.',
  'Pay $20, or $30 later.',
  'From $ 5 to 6 $ a month.',
  'Was $x $ and $ y$ here.',
  'Prices: $5$ each.',
  'Opened $a but never closed.',
])('leaves money alone: %s', async (sentence) => {
  const { container } = show([sentence]);
  expect(container).toHaveTextContent(sentence);
  // Nothing is ever loaded for a message without maths.
  expect(container.querySelector('.math')).toBeNull();
  expect(screen.queryByRole('math')).toBeNull();
});

it('does not join a $ on one line to a $ on the next', () => {
  const { container } = show(['Costs $a', 'and b$ more.']);
  expect(container.querySelector('.math')).toBeNull();
});

it('leaves maths in code alone', async () => {
  show([
    'Write `$x^2$` or `\\(y\\)` to get maths, like $z$.',
    '',
    '```latex',
    '$$ \\frac{a}{b} $$',
    '\\[ c \\]',
    '```',
  ]);
  expect(await formulas(1)).toEqual([{ tex: 'z', display: false }]);
  expect(screen.getByText('$x^2$')).toHaveProperty('tagName', 'CODE');
  expect(screen.getByText('\\(y\\)')).toHaveProperty('tagName', 'CODE');
  expect(screen.getByText(/\$\$ \\frac\{a\}\{b\} \$\$/)).toBeVisible();
});

it('shows a display formula still streaming in as text until it closes', () => {
  const { container } = show(['$$', '\\frac{1}{2']);
  expect(container.querySelector('.math')).toBeNull();
  expect(container).toHaveTextContent(/^\$\$\s*\\frac\{1\}\{2$/);
});
