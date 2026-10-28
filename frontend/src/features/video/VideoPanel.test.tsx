import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ClientController } from '../../api/controller';
import type { VideoProjectView } from '../../api/types';
import type { ClientPlatform } from '../../platform';
import { RuntimeContext } from '../../runtime';
import { OverlayProvider } from '../../ui/overlays';
import VideoPanel from './VideoPanel';
import { VideoProjectCard } from './VideoProjectCard';
import { onVideoRequest, type VideoRequest } from './open-video';

const AT = new Date().toISOString();

function view(patch: Partial<VideoProjectView> = {}): VideoProjectView {
  return {
    project_id: 'vid-1a2b3c4d',
    conversation_id: 'conversation-a',
    title: 'Launch demo',
    latest: 2,
    versions: [
      { number: 2, at: AT, by: 'row-bot', note: 'Shorter intro' },
      { number: 1, at: AT, by: 'row-bot', note: 'First cut' },
    ],
    number: 2,
    scenes: [
      {
        scene_id: 's0a0a0a',
        source: 'signup.mp4',
        start: 1,
        end: 4,
        speed: 1,
        zoom: null,
        card: 'Sign up',
        line: 'First, sign up.',
        voiced: true,
        at: 0,
        length: 3,
      },
      {
        scene_id: 's0b0b0b',
        source: 'pricing.mp4',
        start: 0,
        end: null,
        speed: 2,
        zoom: [0.5, 0, 0.5, 0.5],
        card: '',
        line: 'Then pick a plan.',
        voiced: true,
        at: 3,
        length: 2.5,
      },
    ],
    changed: ['s0a0a0a'],
    music: '',
    music_volume: 0.3,
    captions: false,
    voice_lines: true,
    status: 'made',
    problem: '',
    edit: {
      edit_id: 'edit-00000001',
      conversation_id: 'conversation-a',
      kind: 'video',
      summary: 'Joined 2 videos and cut 2 scenes',
      status: 'saved',
      progress: 1,
      download_received: null,
      download_total: null,
      started_at: AT,
      ended_at: AT,
      seconds: 4,
      duration_seconds: 5.5,
      output_name: 'Launch demo v2.mp4',
      media_ref: 'conversation-a:launch',
      mime_type: 'video/mp4',
      size_bytes: 2048,
      frame_count: 0,
      stopped_by: '',
      problem: '',
      detail: '',
      ffmpeg: null,
    },
    checks: [
      'Length 0:05, 1280×720.',
      'Sound: present (average -18 dB, peak -2 dB).',
    ],
    checks_passed: true,
    ...patch,
  };
}

beforeEach(() => {
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: () => 'blob:launch',
    revokeObjectURL: () => undefined,
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function setup(shown: VideoProjectView = view()) {
  const current = { value: shown };
  const controller = {
    subscribe: () => () => {},
    getSnapshot: () => ({
      status: 'ready',
      conversation: { id: 'conversation-a', revision: '7' },
    }),
    videoProject: vi.fn(async () => current.value),
    changeVideoProject: vi.fn(async (_id: string, change) => {
      current.value = view({
        number: 3,
        latest: 3,
        status: 'voicing',
        scenes: change.scenes.map(
          (scene: VideoProjectView['scenes'][number]) => ({
            ...shown.scenes.find((item) => item.scene_id === scene.scene_id)!,
            ...scene,
          }),
        ),
      });
      return current.value;
    }),
    restoreVideoProject: vi.fn(async () => view({ number: 3, latest: 3 })),
    download: vi.fn(async () => new Blob(['mp4'], { type: 'video/mp4' })),
    controlsSettled: vi.fn(async () => undefined),
    workspaceFor: vi.fn(async () => ({
      revision: '7',
      controls: {
        model_selection: { provider_id: 'fixture', model_ref: 'fixture::m' },
      },
    })),
    intent: vi.fn(async () => ({ status: 'accepted' })),
    showLatest: vi.fn(),
  } as unknown as ClientController & Record<string, ReturnType<typeof vi.fn>>;
  const platform = {} as ClientPlatform;
  render(
    <RuntimeContext.Provider value={{ controller, platform }}>
      <OverlayProvider>
        <VideoPanel
          conversationId="conversation-a"
          projectId="vid-1a2b3c4d"
          visible
        />
      </OverlayProvider>
    </RuntimeContext.Provider>,
  );
  return { controller };
}

it('plays the made video above its scenes, each with its line, speed and what changed', async () => {
  setup();
  const panel = await screen.findByRole('region', {
    name: 'Launch demo video',
  });
  await waitFor(() =>
    expect(
      within(panel).getByLabelText('Launch demo, version 2'),
    ).toHaveAttribute('src', 'blob:launch'),
  );
  const scenes = within(panel).getAllByRole('listitem').slice(0, 2);
  expect(scenes[0]).toHaveAttribute('data-changed', 'true');
  expect(scenes[1]).not.toHaveAttribute('data-changed');
  expect(within(scenes[1]).getByText('2.5 s')).toBeVisible();
  expect(within(scenes[1]).getByLabelText('Scene 2 speed')).toHaveValue('2');
  expect(within(scenes[0]).getByLabelText('Scene 1 line')).toHaveValue(
    'First, sign up.',
  );
  expect(
    within(panel).getByRole('region', { name: 'Review' }),
  ).toHaveTextContent('Ready to share');
});

it('saves edits as a new version made on the one shown, and offers nothing until something changed', async () => {
  const { controller } = setup();
  const line = await screen.findByLabelText('Scene 2 line');
  expect(
    screen.queryByRole('group', { name: 'Unsaved changes' }),
  ).not.toBeInTheDocument();
  await userEvent.clear(line);
  await userEvent.type(line, 'Then pick the plan that fits.');
  await userEvent.click(
    screen.getByRole('button', { name: 'Move scene 2 up' }),
  );
  await userEvent.click(screen.getByRole('button', { name: 'Save and make' }));
  expect(controller.changeVideoProject).toHaveBeenCalledWith('vid-1a2b3c4d', {
    base: 2,
    captions: false,
    note: 'Edited in the panel',
    scenes: [
      expect.objectContaining({
        scene_id: 's0b0b0b',
        line: 'Then pick the plan that fits.',
      }),
      expect.objectContaining({ scene_id: 's0a0a0a', line: 'First, sign up.' }),
    ],
  });
  expect(await screen.findByText('Voicing')).toBeVisible();
});

it('goes back a version in one tap, and Post to X asks Row-Bot in the chat', async () => {
  const { controller } = setup();
  await userEvent.click(
    await screen.findByRole('button', { name: 'Back to the version before' }),
  );
  expect(controller.restoreVideoProject).toHaveBeenCalledWith(
    'vid-1a2b3c4d',
    1,
  );
  await userEvent.click(
    await screen.findByRole('button', { name: 'Post to X' }),
  );
  await waitFor(() => expect(controller.intent).toHaveBeenCalled());
  const [conversation, kind, body] = (
    controller.intent as unknown as ReturnType<typeof vi.fn>
  ).mock.calls[0];
  expect([conversation, kind]).toEqual([
    'conversation-a',
    'conversation.submit',
  ]);
  expect(body.text).toContain('(vid-1a2b3c4d) to X. Show me the post first.');
});

it("the chat's card says how the video stands and opens it beside the chat", async () => {
  const opened: VideoRequest[] = [];
  const stop = onVideoRequest((request) => {
    opened.push(request);
    return true;
  });
  const controller = {
    subscribe: () => () => {},
    getSnapshot: () => ({ status: 'ready' }),
    videoProject: vi.fn(async () => view()),
  } as unknown as ClientController;
  render(
    <RuntimeContext.Provider
      value={{ controller, platform: {} as ClientPlatform }}
    >
      <VideoProjectCard
        projectId="vid-1a2b3c4d"
        conversationId="conversation-a"
      />
    </RuntimeContext.Provider>,
  );
  const card = await screen.findByRole('group', {
    name: 'Launch demo · video',
  });
  expect(card).toHaveTextContent('Made');
  expect(card).toHaveTextContent('Version 2 of 2');
  await userEvent.click(within(card).getByRole('button', { name: 'Open' }));
  expect(opened).toEqual([
    {
      conversationId: 'conversation-a',
      projectId: 'vid-1a2b3c4d',
      title: 'Launch demo',
    },
  ]);
  stop();
});
