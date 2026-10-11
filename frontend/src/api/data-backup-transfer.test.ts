import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { File as NodeFile } from 'node:buffer';
import { webcrypto } from 'node:crypto';
import * as wire from '../../../contracts/client-platform/v1/typescript/client';
import { FixtureTransport } from './fixtures';
import { HttpTransport } from './http';

const CHUNK = 1048576;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function connected() {
  vi.spyOn(wire, 'handshake').mockResolvedValue(
    await new FixtureTransport().connect(),
  );
  const http = new HttpTransport();
  await http.connect();
  return http;
}

function view(received: number, size: number) {
  return {
    upload_id: '00000000-0000-4000-8000-000000000009',
    size_bytes: size,
    received_bytes: received,
    chunk_bytes: CHUNK as typeof CHUNK,
    expires_in_seconds: 1800,
  };
}

it('sends a backup in order and carries on from what the server has after a dropped connection', async () => {
  const http = await connected();
  const size = 3 * CHUNK + 10;
  const begin = vi
    .spyOn(wire, 'beginDataBackupUpload')
    .mockResolvedValue(view(0, size));
  let dropped = false;
  const chunks = vi
    .spyOn(wire, 'uploadDataBackupChunk')
    .mockImplementation(async (_base, _proof, _upload, offset, data) => {
      if (offset === CHUNK && !dropped) {
        dropped = true; // The chunk arrived, but its answer was lost.
        throw new TypeError('Failed to fetch');
      }
      return view(offset + data.size, size);
    });
  const status = vi
    .spyOn(wire, 'getDataBackupUpload')
    .mockResolvedValue(view(2 * CHUNK, size));
  const cancel = vi.spyOn(wire, 'cancelDataBackupUpload');
  vi.useFakeTimers();
  const sent: number[] = [];
  const file = new NodeFile([new Uint8Array(size)], 'Phone/backup:1.zip');
  const sending = http.uploadDataBackup(file as File, (value) =>
    sent.push(value),
  );
  await vi.advanceTimersByTimeAsync(1000);
  await expect(sending).resolves.toBe(view(0, size).upload_id);
  expect(begin.mock.calls[0][2]).toMatchObject({
    name: 'Phone_backup_1.zip',
    size_bytes: size,
  });
  expect(chunks.mock.calls.map((call) => [call[3], call[4].size])).toEqual([
    [0, CHUNK],
    [CHUNK, CHUNK],
    [2 * CHUNK, CHUNK],
    [3 * CHUNK, 10],
  ]);
  expect(status).toHaveBeenCalledTimes(1);
  expect(sent).toEqual([CHUNK, 3 * CHUNK, size]);
  expect(cancel).not.toHaveBeenCalled();
});

it('a refused chunk stops the upload and lets the server delete what it has', async () => {
  const http = await connected();
  vi.spyOn(wire, 'beginDataBackupUpload').mockResolvedValue(view(0, 2 * CHUNK));
  const chunks = vi
    .spyOn(wire, 'uploadDataBackupChunk')
    .mockRejectedValue({ code: 'upload_expired', retryable: false });
  const cancel = vi
    .spyOn(wire, 'cancelDataBackupUpload')
    .mockResolvedValue({ cancelled: true });
  const file = new NodeFile([new Uint8Array(2 * CHUNK)], 'backup.zip');
  await expect(http.uploadDataBackup(file as File)).rejects.toEqual({
    code: 'upload_expired',
    retryable: false,
  });
  expect(chunks).toHaveBeenCalledTimes(1);
  expect(cancel).toHaveBeenCalledWith(
    expect.anything(),
    expect.anything(),
    view(0, 1).upload_id,
  );
});

it('a downloaded backup shorter than the server said is never handed over', async () => {
  const http = await connected();
  const respond = (length: string) =>
    new Response(new Uint8Array(10), {
      headers: {
        'Content-Type': 'application/zip',
        'Content-Length': length,
        'Content-Encoding': 'identity',
      },
    });
  const fetch = vi.fn().mockResolvedValueOnce(respond('10'));
  vi.stubGlobal('fetch', fetch);
  const whole = await http.downloadDataBackup('Row-Bot backup.zip');
  expect(whole.size).toBe(10);
  expect(String(fetch.mock.calls[0][0])).toContain(
    '/api/v1/data/backup/download?name=Row-Bot+backup.zip',
  );
  fetch.mockResolvedValueOnce(respond('20'));
  await expect(http.downloadDataBackup('Row-Bot backup.zip')).rejects.toThrow(
    'protocol_incompatible',
  );
});
