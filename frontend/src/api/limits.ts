/** The server's attachment limits (application/attachments.py). */
export const ATTACHMENT_LIMITS = {
  fileBytes: 200 * 1024 * 1024,
  fileMegabytes: 200,
  files: 32,
  batchBytes: 1024 * 1024 * 1024,
  batchMegabytes: 1024,
} as const;
