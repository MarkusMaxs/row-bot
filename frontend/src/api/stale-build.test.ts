import { describe, expect, it, vi } from 'vitest';
import { reloadAfterUpdate } from './stale-build';

function page(served: string | Error, running = 'index-Old111.js') {
  const script = document.createElement('script');
  script.type = 'module';
  script.src = `/app-v2/assets/${running}`;
  const target = Object.assign(new EventTarget(), {
    fetch: vi.fn(async () => {
      if (served instanceof Error) throw served;
      return new Response(
        `<script type="module" crossorigin src="/app-v2/assets/${served}"></script>`,
      );
    }),
    document: { querySelector: () => script },
    location: { reload: vi.fn() },
  });
  reloadAfterUpdate(target);
  return target;
}
async function failedLoad(target: EventTarget) {
  target.dispatchEvent(new Event('vite:preloadError'));
  await vi.waitFor(() => undefined);
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

describe('a page left open across an update (#568)', () => {
  it('reloads to the new build when a part of the old one is gone', async () => {
    const target = page('index-New222.js');
    await failedLoad(target);
    expect(target.location.reload).toHaveBeenCalledOnce();
  });
  it('keeps the page when the build is the same or Row-Bot is unreachable', async () => {
    const same = page('index-Old111.js');
    await failedLoad(same);
    const away = page(new TypeError('Failed to fetch'));
    await failedLoad(away);
    expect(same.location.reload).not.toHaveBeenCalled();
    expect(away.location.reload).not.toHaveBeenCalled();
  });
});
