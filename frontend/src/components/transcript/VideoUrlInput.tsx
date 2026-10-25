import { useState, useCallback, useRef, useEffect, type FormEvent } from 'react';
import { CheckCircle2, XCircle, Loader2, ChevronRight, X, Clipboard } from 'lucide-react';

function isChannelHandle(input: string): boolean {
  const trimmed = input.trim();
  if (trimmed.startsWith('@')) return true;
  if (/^UC[a-zA-Z0-9_-]{22,}$/.test(trimmed)) return true;
  if (trimmed.includes('/channel/') || trimmed.includes('/@')) return true;
  return false;
}

function cleanHandle(input: string): string {
  let h = input.trim();
  if (h.includes('youtube.com/channel/')) h = h.split('/channel/')[1]?.split('/')[0] || h;
  if (h.includes('youtube.com/@')) h = h.split('/@')[1]?.split('/')[0] || h;
  if (h.startsWith('@')) h = h.slice(1);
  return h;
}

interface ValidationResult {
  valid: boolean;
  video_id: string | null;
  normalized_url: string | null;
  url_type: string | null;
  original_url: string | null;
  error: string | null;
}

interface VideoUrlInputProps {
  onValidUrl?: (videoId: string, normalizedUrl: string) => void;
  onChannelDetected?: (handle: string) => void;
  onSubmit?: (videoId: string, normalizedUrl: string) => void;
  isProcessing?: boolean;
}

export default function VideoUrlInput({
  onValidUrl,
  onChannelDetected,
  onSubmit,
  isProcessing = false,
}: VideoUrlInputProps) {
  const [url, setUrl] = useState('');
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [touched, setTouched] = useState(false);
  const debounceRef = useRef<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const validateUrl = useCallback(async (input: string): Promise<ValidationResult | null> => {
    const trimmed = input.trim();
    if (!trimmed) {
      setValidation(null);
      return null;
    }

    // Detect channel handles client-side
    if (isChannelHandle(trimmed)) {
      const handle = cleanHandle(trimmed);
      const res: ValidationResult = {
        valid: true,
        video_id: null,
        normalized_url: null,
        url_type: 'channel',
        original_url: trimmed,
        error: null,
      };
      setValidation(res);
      if (onChannelDetected) {
        onChannelDetected(handle);
      }
      return res;
    }

    setLoading(true);
    try {
      const resp = await fetch(`/api/validate-url?url=${encodeURIComponent(trimmed)}`);
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        const res: ValidationResult = {
          valid: false,
          video_id: null,
          normalized_url: null,
          url_type: null,
          original_url: trimmed,
          error: body?.error || `Server returned ${resp.status} ${resp.statusText}. Please try again.`,
        };
        setValidation(res);
        return res;
      }
      const data: ValidationResult = await resp.json();
      if (typeof data.valid !== 'boolean') {
        const res: ValidationResult = {
          valid: false,
          video_id: null,
          normalized_url: null,
          url_type: null,
          original_url: trimmed,
          error: 'Unexpected response from server. Please try again.',
        };
        setValidation(res);
        return res;
      }
      setValidation(data);
      if (data.valid && data.video_id && data.normalized_url) {
        onValidUrl?.(data.video_id, data.normalized_url);
      }
      return data;
    } catch (err) {
      const message = err instanceof TypeError
        ? 'Could not reach the server. Check your connection and try again.'
        : `Validation request failed: ${err instanceof Error ? err.message : 'Unknown error'}.`;
      const res: ValidationResult = {
        valid: false,
        video_id: null,
        normalized_url: null,
        url_type: null,
        original_url: trimmed,
        error: message,
      };
      setValidation(res);
      return res;
    } finally {
      setLoading(false);
    }
  }, [onValidUrl, onChannelDetected]);

  const handleChange = (value: string) => {
    setUrl(value);
    setTouched(true);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => validateUrl(value), 400);
  };

  const handlePaste = async () => {
    try {
      const text = await navigator.clipboard.readText();
      handleChange(text);
    } catch {
      // Clipboard API not available
    }
  };

  const handleClear = () => {
    setUrl('');
    setValidation(null);
    setTouched(false);
    inputRef.current?.focus();
  };

  const triggerSubmit = useCallback(async () => {
    if (isProcessing) return;
    const value = (inputRef.current?.value ?? url).trim();
    if (!value) return;

    if (validation?.valid && validation.video_id) {
      if (onSubmit) {
        onSubmit(validation.video_id, validation.normalized_url || value);
      }
      return;
    }

    const res = await validateUrl(value);
    if (res && res.valid && res.video_id && onSubmit) {
      onSubmit(res.video_id, res.normalized_url || value);
    }
  }, [isProcessing, url, validation, onSubmit, validateUrl]);

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    triggerSubmit();
  };

  const handleButtonClick = (e: React.MouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    triggerSubmit();
  };

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  const statusIcon = () => {
    if (loading) return <Loader2 size={16} className="text-gray-400 animate-spin" />;
    if (!touched || !url) return null;
    if (validation?.valid) return <CheckCircle2 size={16} className="text-emerald-500" />;
    if (validation && !validation.valid) return <XCircle size={16} className="text-red-500" />;
    return null;
  };

  const borderColor = () => {
    if (!touched || !url || loading) return 'border-gray-300 dark:border-gray-700 focus:border-blue-600';
    if (validation && !validation.valid) return 'border-red-400 dark:border-red-700';
    return 'border-gray-300 dark:border-gray-700 focus:border-blue-600';
  };

  return (
    <form onSubmit={handleSubmit} noValidate>
      <div className="flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <input
            ref={inputRef}
            type="text"
            value={url}
            onChange={(e) => handleChange(e.target.value)}
            onBlur={() => setTouched(true)}
            placeholder="Paste a YouTube link"
            aria-label="YouTube video URL"
            disabled={isProcessing}
            className={`h-12 w-full pl-4 pr-20 rounded-xl border text-[15px] transition-all outline-none bg-white dark:bg-gray-800 text-gray-900 dark:text-white placeholder:text-gray-400 focus:ring-4 focus:ring-blue-600/15 ${borderColor()} ${isProcessing ? 'opacity-60 cursor-not-allowed' : ''}`}
            autoFocus
            autoComplete="off"
            spellCheck={false}
          />
          <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-1">
            {statusIcon()}
            {url && (
              <button
                type="button"
                onClick={handleClear}
                disabled={isProcessing}
                className="p-1 rounded-md text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-all-200 disabled:opacity-50"
                aria-label="Clear input"
              >
                <X size={14} />
              </button>
            )}
            <button
              type="button"
              onClick={handlePaste}
              disabled={isProcessing}
              className="p-1 rounded-md text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-all-200 disabled:opacity-50"
              aria-label="Paste from clipboard"
            >
              <Clipboard size={14} />
            </button>
          </div>
        </div>

        <button
          type="button"
          onClick={handleButtonClick}
          disabled={!validation?.valid || isProcessing}
          className={`inline-flex items-center justify-center gap-1.5 h-12 px-6 rounded-full font-medium text-[15px] transition-colors ${
            validation?.valid && !isProcessing
              ? 'bg-blue-600 text-white hover:bg-blue-700 active:bg-blue-800 cursor-pointer'
              : 'bg-gray-200 dark:bg-gray-800 text-gray-400 dark:text-gray-600 cursor-not-allowed'
          }`}
        >
          {isProcessing ? (
            <Loader2 size={16} className="animate-spin" />
          ) : (
            <>
              Get Transcript
              <ChevronRight size={16} />
            </>
          )}
        </button>
      </div>

      {touched && url && !loading && validation && !validation.valid && (
        <p className="mt-2 text-xs text-red-500">{validation.error || 'Unsupported URL'}</p>
      )}
    </form>
  );
}
