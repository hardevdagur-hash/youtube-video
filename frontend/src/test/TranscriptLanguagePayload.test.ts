import { describe, it, expect, vi, afterEach } from 'vitest';
import { transcriptService } from '../services/TranscriptService';

function ok(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

describe('transcript output_language reaches the backend unchanged', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(['original', 'en', 'hi'] as const)('single video sends output_language=%s', async (mode) => {
    const fetchMock = vi.spyOn(window, 'fetch').mockResolvedValue(ok({ success: true }));
    await transcriptService.fetchUnifiedTranscript('https://youtu.be/dQw4w9WgXcQ', mode);
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init?.body)).output_language).toBe(mode);
  });

  it.each(['original', 'en', 'hi'] as const)('simple channel mode sends output_language=%s', async (mode) => {
    const fetchMock = vi.spyOn(window, 'fetch').mockResolvedValue(ok({ success: true, data: { videos: [] } }));
    await transcriptService.fetchChannelTranscriptsSimple('physicschannel', 5, 5, true, mode);
    const url = new URL(String(fetchMock.mock.calls[0][0]), 'http://localhost');
    expect(url.searchParams.get('output_language')).toBe(mode);
  });

  it('simple channel mode defaults to original (never silently English)', async () => {
    const fetchMock = vi.spyOn(window, 'fetch').mockResolvedValue(ok({ success: true, data: { videos: [] } }));
    await transcriptService.fetchChannelTranscriptsSimple('physicschannel');
    const url = new URL(String(fetchMock.mock.calls[0][0]), 'http://localhost');
    expect(url.searchParams.get('output_language')).toBe('original');
  });

  it.each(['original', 'en', 'hi'])('background job sends output_language=%s', async (mode) => {
    const fetchMock = vi.spyOn(window, 'fetch').mockResolvedValue(ok({ success: true, data: { job_id: 'abc' } }));
    await transcriptService.startTranscriptJob('physicschannel', 10, false, null, null, mode).catch(() => undefined);
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init?.body)).output_language).toBe(mode);
  });
});
