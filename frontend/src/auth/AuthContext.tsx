import { createContext, useCallback, useContext, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { LogIn } from 'lucide-react';
import { Button } from '../components/ui';

/**
 * Session auth for the SPA.
 *
 * The backend issues an HttpOnly, SameSite=Strict session cookie on login, so the
 * browser attaches it automatically to same-origin fetches and <a href> downloads.
 * Any /api response with 401 (expired or revoked session) flips the app back to the
 * login form via the `auth:unauthorized` event.
 */

export interface AuthUser {
  username: string;
  role: 'admin' | 'user';
}

/** Server-enforced caps reported by /api/auth/me (defaults match the backend defaults). */
export interface ServerLimits {
  maxVideosPerJob: number;
  maxVideosSync: number;
  maxActiveJobsPerUser: number;
  /** Channel videos are eligible when min <= duration < max (seconds). */
  channelMinVideoSeconds: number;
  channelMaxVideoSeconds: number;
}

export const DEFAULT_LIMITS: ServerLimits = {
  maxVideosPerJob: 100,
  maxVideosSync: 25,
  maxActiveJobsPerUser: 2,
  channelMinVideoSeconds: 180,
  channelMaxVideoSeconds: 1800,
};

/** "3:00" / "1:30:00" style label for a duration in seconds. */
export function formatDurationLabel(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

/** Human description of the channel eligibility window, e.g. "3:00 – 30:00". */
export function channelWindowLabel(limits: ServerLimits): string {
  return `${formatDurationLabel(limits.channelMinVideoSeconds)} – ${formatDurationLabel(limits.channelMaxVideoSeconds)}`;
}

type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

interface AuthContextValue {
  status: AuthStatus;
  user: AuthUser | null;
  limits: ServerLimits;
  login: (username: string, password: string) => Promise<string | null>;
  logout: () => Promise<void>;
}

const UNAUTHORIZED_EVENT = 'auth:unauthorized';
const API_BASE = import.meta.env.VITE_API_BASE || '';

const AuthContext = createContext<AuthContextValue | null>(null);

let interceptorInstalled = false;

/** Wrap window.fetch once so every API call reports expired sessions. */
export function installAuthInterceptor(): void {
  if (interceptorInstalled || typeof window === 'undefined') return;
  interceptorInstalled = true;
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const response = await originalFetch(input, init);
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (response.status === 401 && url.includes('/api/') && !url.includes('/api/auth/')) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    return response;
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<AuthUser | null>(null);
  const [limits, setLimits] = useState<ServerLimits>(DEFAULT_LIMITS);

  /** Load the signed-in principal and the server limits; returns false when not signed in. */
  const loadMe = useCallback(async (): Promise<boolean> => {
    const resp = await fetch(`${API_BASE}/api/auth/me`, { credentials: 'same-origin' });
    if (!resp.ok) return false;
    const data = await resp.json();
    setUser({ username: data.user.username, role: data.user.role });
    if (data.limits) {
      setLimits({
        maxVideosPerJob: Number(data.limits.max_videos_per_job) || DEFAULT_LIMITS.maxVideosPerJob,
        maxVideosSync: Number(data.limits.max_videos_sync) || DEFAULT_LIMITS.maxVideosSync,
        maxActiveJobsPerUser: Number(data.limits.max_active_jobs_per_user) || DEFAULT_LIMITS.maxActiveJobsPerUser,
        channelMinVideoSeconds:
          Number(data.limits.channel_min_video_seconds) || DEFAULT_LIMITS.channelMinVideoSeconds,
        channelMaxVideoSeconds:
          Number(data.limits.channel_max_video_seconds) || DEFAULT_LIMITS.channelMaxVideoSeconds,
      });
    }
    return true;
  }, []);

  useEffect(() => {
    let cancelled = false;
    loadMe()
      .then((ok) => {
        if (!cancelled) setStatus(ok ? 'authenticated' : 'anonymous');
      })
      .catch(() => {
        if (!cancelled) setStatus('anonymous');
      });

    const onUnauthorized = () => {
      setUser(null);
      setStatus('anonymous');
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => {
      cancelled = true;
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    };
  }, [loadMe]);

  const login = useCallback(async (username: string, password: string): Promise<string | null> => {
    try {
      const resp = await fetch(`${API_BASE}/api/auth/login`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        return data.error || 'Sign in failed.';
      }
      setUser({ username: data.user.username, role: data.user.role });
      setStatus('authenticated');
      // Limits are not part of the login response; on failure the defaults stay in place.
      loadMe().catch(() => undefined);
      return null;
    } catch {
      return 'Cannot reach the server.';
    }
  }, [loadMe]);

  const logout = useCallback(async () => {
    try {
      await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST', credentials: 'same-origin' });
    } finally {
      setUser(null);
      setStatus('anonymous');
    }
  }, []);

  return <AuthContext.Provider value={{ status, user, limits, login, logout }}>{children}</AuthContext.Provider>;
}

const SIGNED_OUT: AuthContextValue = {
  status: 'anonymous',
  user: null,
  limits: DEFAULT_LIMITS,
  login: async () => 'Authentication is not available.',
  logout: async () => {},
};

export function useAuth(): AuthContextValue {
  // Components rendered outside <AuthProvider> (e.g. isolated tests) behave as signed out.
  return useContext(AuthContext) ?? SIGNED_OUT;
}

/** Renders children only for signed-in users; otherwise shows the sign-in form in place. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  if (status === 'loading') {
    return (
      <div className="flex items-center justify-center min-h-[200px]">
        <div className="animate-pulse text-sm text-gray-400">Loading...</div>
      </div>
    );
  }
  if (status === 'anonymous') return <LoginForm />;
  return <>{children}</>;
}

export function LoginForm() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(await login(username.trim(), password));
    setSubmitting(false);
  };

  const inputCls =
    'w-full px-4 py-2.5 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm outline-none focus:border-emerald-400 focus:ring-4 focus:ring-emerald-100 dark:focus:ring-emerald-900/30';

  return (
    <div className="flex items-center justify-center min-h-[70vh] px-4 pt-24">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 p-8 rounded-2xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-sm"
        aria-label="Sign in"
      >
        <h1 className="text-xl font-bold text-gray-900 dark:text-white">Sign in</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">Sign in to fetch and export transcripts.</p>
        <div>
          <label htmlFor="login-username" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
            Username
          </label>
          <input
            id="login-username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            maxLength={64}
            className={inputCls}
          />
        </div>
        <div>
          <label htmlFor="login-password" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
            Password
          </label>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            maxLength={256}
            className={inputCls}
          />
        </div>
        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
        <Button type="submit" disabled={submitting} icon={<LogIn size={16} />} className="w-full">
          {submitting ? 'Signing in…' : 'Sign in'}
        </Button>
      </form>
    </div>
  );
}
