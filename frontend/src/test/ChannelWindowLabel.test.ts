import { describe, it, expect } from 'vitest';
import { DEFAULT_LIMITS, channelWindowLabel, formatDurationLabel } from '../auth/AuthContext';

describe('channel eligibility window label', () => {
  it('formats durations as m:ss or h:mm:ss', () => {
    expect(formatDurationLabel(180)).toBe('3:00');
    expect(formatDurationLabel(1800)).toBe('30:00');
    expect(formatDurationLabel(5400)).toBe('1:30:00');
    expect(formatDurationLabel(-5)).toBe('0:00');
  });

  it('describes the default window like the backend default (3:00 – 30:00)', () => {
    expect(channelWindowLabel(DEFAULT_LIMITS)).toBe('3:00 – 30:00');
  });

  it('follows server-configured limits', () => {
    expect(channelWindowLabel({ ...DEFAULT_LIMITS, channelMinVideoSeconds: 60, channelMaxVideoSeconds: 7200 })).toBe(
      '1:00 – 2:00:00',
    );
  });
});
