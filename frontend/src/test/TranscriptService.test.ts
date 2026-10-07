import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { transcriptService } from '../services/TranscriptService';
import { getLanguageLabel } from '../types';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('TranscriptService', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('posts the selected output language to the unified transcript endpoint', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ success: true, output_language: 'original', transcript: 'x' }));
    await transcriptService.fetchUnifiedTranscript('https://youtu.be/dQw4w9WgXcQ', 'original');
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/transcript');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ video_url: 'https://youtu.be/dQw4w9WgXcQ', output_language: 'original' });
  });

  it('surfaces the backend error code and retryability', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ success: false, error_code: 'INVALID_YOUTUBE_URL', message: 'bad url', retryable: false }, 400),
    );
    await expect(transcriptService.fetchUnifiedTranscript('nope', 'en')).rejects.toMatchObject({
      message: 'bad url',
      error_code: 'INVALID_YOUTUBE_URL',
      retryable: false,
    });
  });

  it('encodes the channel handle and output language for channel transcripts', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ success: true, data: { videos: [] } }));
    await transcriptService.fetchChannelTranscriptsSimple('@my chan', 10, 2, false, 'hi');
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/channel/%40my%20chan/transcripts?limit=10&concurrency=2&allow_whisper=false&output_language=hi');
  });

  it('sends the job output language in the job creation body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ success: true, data: { job_id: 'abcdef012345' } }));
    const job = await transcriptService.startTranscriptJob('chan', 25, false, '2024-01-01', null, 'original');
    expect(job.job_id).toBe('abcdef012345');
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/channel/chan/transcript-job');
    expect(JSON.parse(init.body)).toEqual({
      max_videos: 25,
      force_refresh: false,
      published_after: '2024-01-01',
      published_before: null,
      output_language: 'original',
    });
  });

  it('builds an encoded job download URL', () => {
    expect(transcriptService.getJobDownloadUrl('abcdef012345')).toBe('/api/transcript/jobs/abcdef012345/download');
  });
});

describe('getLanguageLabel', () => {
  it('should return English for en', () => {
    expect(getLanguageLabel('en')).toBe('English');
  });

  it('should return Hindi for hi', () => {
    expect(getLanguageLabel('hi')).toBe('Hindi');
  });

  it('should return Spanish for es', () => {
    expect(getLanguageLabel('es')).toBe('Spanish');
  });

  it('should uppercase unknown codes', () => {
    expect(getLanguageLabel('xyz')).toBe('XYZ');
  });
});
