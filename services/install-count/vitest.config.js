import path from 'node:path';
import { cloudflareTest, readD1Migrations } from '@cloudflare/vitest-plugin';
import { defineConfig } from 'vitest/config';

// The Worker runs in workerd (Miniflare) with a local D1 and rate limiter; nothing leaves this computer.
export default defineConfig(async () => {
  const migrations = await readD1Migrations(path.join(import.meta.dirname, 'migrations'));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: path.join(import.meta.dirname, 'wrangler.toml') },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            ID_HMAC_KEY: 'test-only-hmac-key',
            STATS_TOKEN: 'test-only-stats-token',
          },
        },
      }),
    ],
    test: { setupFiles: [path.join(import.meta.dirname, 'test/apply-migrations.js')] },
  };
});
