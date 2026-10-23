import { applyD1Migrations, env } from 'cloudflare:test';
import { beforeEach } from 'vitest';

await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);

// Each test starts with an empty database.
beforeEach(async () => {
  await env.DB.batch([env.DB.prepare('DELETE FROM pings'), env.DB.prepare('DELETE FROM totals')]);
});
