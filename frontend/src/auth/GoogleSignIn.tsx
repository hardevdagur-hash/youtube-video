/**
 * "Continue with Google" for the sign-in page.
 *
 * The button is a plain link to the backend's /api/auth/google/start: the whole OAuth
 * flow (state, nonce, PKCE, code exchange, ID-token verification) happens server-side,
 * which then sets the application's normal HttpOnly session cookie and redirects back
 * to /transcript. Nothing about the Google identity is handled or trusted in the browser.
 */

const API_BASE = import.meta.env.VITE_API_BASE || '';

export const GOOGLE_START_URL = `${API_BASE}/api/auth/google/start`;

/** Messages for the coarse ?auth_error=<code> the backend appends after a failed attempt. */
export const GOOGLE_AUTH_ERRORS: Record<string, string> = {
  cancelled: 'Google sign-in was cancelled.',
  not_allowed: 'This Google account is not allowed to use this application.',
  unavailable: 'Google sign-in is not available right now. Please try again later.',
  rate_limited: 'Too many sign-in attempts. Please wait a minute and try again.',
  failed: 'Google sign-in failed. Please try again.',
};

/**
 * Read and remove ?auth_error= from the address bar (so a reload does not repeat it).
 * Unknown codes map to the generic message; the raw value is never displayed.
 */
export function consumeGoogleAuthError(): string | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(window.location.href);
  const code = url.searchParams.get('auth_error');
  if (code === null) return null;
  url.searchParams.delete('auth_error');
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
  return GOOGLE_AUTH_ERRORS[code] ?? GOOGLE_AUTH_ERRORS.failed;
}

/** Ask the server which sign-in methods are enabled; any failure means "no Google". */
export async function fetchGoogleEnabled(): Promise<boolean> {
  try {
    const resp = await fetch(`${API_BASE}/api/auth/providers`, { credentials: 'same-origin' });
    if (!resp.ok) return false;
    const data = await resp.json();
    return data?.providers?.google === true;
  } catch {
    return false;
  }
}

/** Google's multi-colour "G" mark (official colours), as required by Google's branding guidelines. */
function GoogleLogo() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}

export function GoogleSignInButton() {
  return (
    <a
      href={GOOGLE_START_URL}
      className="w-full inline-flex items-center justify-center gap-3 px-4 py-2.5 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm font-medium text-gray-800 dark:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors outline-none focus-visible:ring-4 focus-visible:ring-emerald-100 dark:focus-visible:ring-emerald-900/30"
    >
      <GoogleLogo />
      Continue with Google
    </a>
  );
}

export function SignInDivider() {
  return (
    <div className="flex items-center gap-3" role="separator" aria-label="or">
      <span className="h-px flex-1 bg-gray-200 dark:bg-gray-700" />
      <span className="text-xs font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">or</span>
      <span className="h-px flex-1 bg-gray-200 dark:bg-gray-700" />
    </div>
  );
}
