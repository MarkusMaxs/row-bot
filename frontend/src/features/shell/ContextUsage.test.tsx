import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import ContextUsage, { describeConversationUsage } from './ContextUsage';

it('shows no ring before the first measurement, so nothing looks like it is loading', () => {
  const { container, rerender } = render(<ContextUsage />);
  expect(container).toBeEmptyDOMElement();
  rerender(
    <ContextUsage
      usage={{
        conversation_id: 'a',
        state: 'unknown',
        freshness: 'unknown',
        status: 'unavailable',
        estimated_input_tokens: null,
        usable_input_tokens: null,
        compact_at_tokens: null,
        native_window_tokens: null,
        effective_limit_tokens: null,
        model_ref: null,
        last_confirmed_input_tokens: null,
        scope: 'unknown',
        capacity_state: 'unknown',
      }}
    />,
  );
  expect(container).toBeEmptyDOMElement();
  // Measured and empty: the track alone, no lone dot of an arc.
  rerender(
    <ContextUsage
      usage={{
        conversation_id: 'a',
        state: 'live',
        freshness: 'current',
        status: 'ready',
        estimated_input_tokens: 0,
        usable_input_tokens: 100000,
        compact_at_tokens: null,
        native_window_tokens: 128000,
        effective_limit_tokens: 100000,
        model_ref: 'fake/model',
        last_confirmed_input_tokens: null,
        scope: 'agent',
        capacity_state: 'ready',
      }}
    />,
  );
  expect(screen.getByText('Context 0%')).toBeInTheDocument();
  expect(container.querySelector('.context-ring-fill')).toBeNull();
});

it('labels stale usage without claiming a current measurement', () => {
  const { rerender } = render(<ContextUsage />);
  rerender(
    <ContextUsage
      usage={{
        conversation_id: 'a',
        state: 'stale',
        freshness: 'stale',
        status: 'ready',
        estimated_input_tokens: 12345,
        usable_input_tokens: 100000,
        compact_at_tokens: 75000,
        native_window_tokens: 128000,
        effective_limit_tokens: 100000,
        model_ref: 'fake/model',
        last_confirmed_input_tokens: 0,
        scope: 'agent',
        capacity_state: 'ready',
      }}
    />,
  );
  expect(screen.getByText('Context ~12%')).toBeInTheDocument();
  expect(screen.getByRole('meter', { name: 'Context usage' })).toHaveAttribute(
    'aria-valuenow',
    '12345',
  );
  // The ring marks the compaction threshold (75%) with a tick.
  expect(document.querySelector('.context-ring-threshold')).not.toBeNull();
  expect(
    screen.getByRole('button', {
      name: 'Context ~12%; automatic compaction threshold marker',
    }),
  ).toBeVisible();
  fireEvent.click(screen.getByText('Context ~12%'));
  expect(
    screen.getByText('Last provider-confirmed input: 0 tokens.'),
  ).toBeInTheDocument();
});

it('shows compaction and failure states without pretending capacity exists', () => {
  const usage = {
    conversation_id: 'a',
    state: 'live' as const,
    freshness: 'current' as const,
    status: 'compacting' as const,
    estimated_input_tokens: 80,
    usable_input_tokens: 100,
    compact_at_tokens: 75,
    native_window_tokens: 128,
    effective_limit_tokens: 100,
    model_ref: 'fake/model',
    last_confirmed_input_tokens: null,
    scope: 'chat_only' as const,
    capacity_state: 'ready',
  };
  const { rerender } = render(<ContextUsage usage={usage} />);
  expect(screen.getByText('Compacting context…')).toBeInTheDocument();
  rerender(<ContextUsage usage={{ ...usage, status: 'failed' as const }} />);
  expect(
    screen.getByText('Context 80% · compaction failed'),
  ).toBeInTheDocument();
});

const measured = {
  conversation_id: 'a',
  state: 'live' as const,
  freshness: 'current' as const,
  status: 'ready' as const,
  estimated_input_tokens: 12000,
  usable_input_tokens: 100000,
  compact_at_tokens: 75000,
  native_window_tokens: 128000,
  effective_limit_tokens: 100000,
  model_ref: 'openrouter/anthropic/claude-sonnet-4.5',
  last_confirmed_input_tokens: null,
  scope: 'agent' as const,
  capacity_state: 'ready',
};

it('shows the tokens this conversation used, the share read from cache and a reported cost', () => {
  render(
    <ContextUsage
      usage={{
        ...measured,
        conversation_usage: {
          calls: 9,
          input_tokens: 1_200_000,
          output_tokens: 34_567,
          cache_read_percent: 82,
          cost_usd: 0.4213,
        },
      }}
    />,
  );
  fireEvent.click(screen.getByText('Context 12%'));
  expect(
    screen.getByText(
      'This conversation: 1,234,567 tokens used, 82% of input read from cache. Cost reported by the provider: $0.42.',
    ),
  ).toBeInTheDocument();
  expect(
    describeConversationUsage({
      calls: 9,
      input_tokens: 1_200_000,
      output_tokens: 34_567,
      cache_read_percent: 82,
      cost_usd: 0.4213,
    })?.hint,
  ).toBe('1.2M tokens used, 82% from cache');
});

it('leaves out what the provider did not report, and says nothing before the first call', () => {
  const { rerender } = render(
    <ContextUsage
      usage={{
        ...measured,
        conversation_usage: {
          calls: 2,
          input_tokens: 900,
          output_tokens: 100,
          cache_read_percent: null,
          cost_usd: null,
        },
      }}
    />,
  );
  fireEvent.click(screen.getByText('Context 12%'));
  expect(
    screen.getByText('This conversation: 1,000 tokens used.'),
  ).toBeInTheDocument();
  expect(screen.queryByText(/from cache|Cost reported/)).toBeNull();
  rerender(<ContextUsage usage={measured} />);
  expect(screen.queryByText(/This conversation:/)).toBeNull();
  expect(describeConversationUsage(null)).toBeNull();
  expect(
    describeConversationUsage({
      calls: 1,
      input_tokens: 10,
      output_tokens: 1,
      cache_read_percent: 0,
      cost_usd: 0.0042,
    }),
  ).toEqual({
    hint: '11 tokens used, 0% from cache',
    detail:
      'This conversation: 11 tokens used, 0% of input read from cache. Cost reported by the provider: $0.0042.',
  });
});
