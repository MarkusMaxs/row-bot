import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import type { ClientController } from '../../api/controller';
import type { AppView, IntegrationDetail } from '../../api/types';
import type { ClientPlatform } from '../../platform';
import { RuntimeContext } from '../../runtime';
import { WorkspaceActionsContext } from '../shell/workspace-actions';
import AppsArea from './AppsArea';

// B398: Microsoft 365 needs an admin and Copilot Credits; its page says so
// before anyone starts, and points one mailbox to Outlook. (Its own file: the
// app catalog is read once per page load.)

function app(id: string, name: string, extra: Partial<AppView> = {}): AppView {
  return {
    id,
    name,
    publisher: 'Microsoft',
    category: 'productivity',
    icon: `letter:${name[0]}`,
    verified: true,
    featured_rank: 6,
    summary: `${name} summary.`,
    jobs: [],
    example_prompts: [],
    variants: ['hosted_mcp'],
    auth: 'oauth',
    docs_url: '',
    key_url: '',
    icon_license: null,
    ...extra,
  };
}

const detail = {
  entry: {
    id: 'mcp:official:com.microsoft/workiq-admintools@1.0.0',
    kind: 'mcp',
    parent_id: null,
    name: 'Admin MCP Server',
    description: 'Admin tools.',
    app: {
      id: 'microsoft-365',
      name: 'Microsoft 365',
      publisher: 'Microsoft',
      category: 'productivity',
      icon: 'letter:M',
      verified: true,
      featured_rank: 6,
    },
    icon: 'letter:M',
    verified: true,
    source: 'official',
    method: 'hosted_sign_in',
    publisher: 'Microsoft',
    version: '1.0.0',
    installed: false,
    enabled: false,
    account_label: '',
    compatibility: 'supported',
    evidence: 'listed',
    tested_with_row_bot: false,
    lifecycle: 'available',
    readiness: null,
    blockers: [],
    next_action: { kind: 'connect', label: 'Connect' },
    attributions: [],
    children: [],
  },
  plan: null,
  about: {
    license: '',
    source_url: '',
    pin: '',
    identifier: 'com.microsoft/workiq-admintools',
    destination: 'https://agent365.svc.cloud.microsoft',
    runs_locally: false,
    saved_key: false,
    signs_in: true,
    signed_in: false,
    requirements: [],
    access: null,
    package: '',
    files: [],
    profiles: [],
    actions: [],
  },
} as unknown as IntegrationDetail;

function Area() {
  const location = useLocation();
  const [, , , ...rest] = location.pathname.split('/');
  return (
    <AppsArea kind="app" item={rest.join('/')} editor={() => <p>Editor</p>} />
  );
}

it('says what Microsoft 365 needs before anyone starts, and points one mailbox to Outlook', async () => {
  const controller = {
    integrationIcons: vi.fn(async () => ({ schema_version: 1, items: [] })),
    integrationApps: vi.fn(async () => ({
      schema_version: 1,
      items: [
        app('microsoft-365', 'Microsoft 365', {
          note: 'Needs a Microsoft 365 admin to turn on Work IQ, and Copilot Credits. For one mailbox, use Outlook (your own Microsoft app).',
          see_also: 'outlook',
        }),
        app('outlook', 'Outlook', { variants: ['account'] }),
      ],
    })),
    integrationDetail: vi.fn(async () => detail),
    integrationItems: vi.fn(async () => ({
      schema_version: 1,
      revision: 'a'.repeat(64),
      items: [],
      total: 0,
      next_cursor: null,
      sources: [],
    })),
  } as unknown as ClientController;
  render(
    <RuntimeContext.Provider
      value={{
        controller,
        platform: { openExternal: vi.fn() } as unknown as ClientPlatform,
      }}
    >
      <WorkspaceActionsContext.Provider
        value={{ resetLayout: vi.fn(), newChat: vi.fn() }}
      >
        <MemoryRouter
          initialEntries={[
            `/settings/apps/item?id=${encodeURIComponent(detail.entry.id)}`,
          ]}
        >
          <Routes>
            <Route path="settings/:setting/*" element={<Area />} />
          </Routes>
        </MemoryRouter>
      </WorkspaceActionsContext.Provider>
    </RuntimeContext.Provider>,
  );
  expect(
    await screen.findByText(/Needs a Microsoft 365 admin to turn on Work IQ/),
  ).toBeVisible();
  expect(screen.getByRole('link', { name: 'Open Outlook' })).toHaveAttribute(
    'href',
    '/settings/apps/outlook',
  );
});
