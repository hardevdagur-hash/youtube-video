import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { AuthProvider, RequireAuth } from '../auth/AuthContext';
import { GOOGLE_AUTH_ERRORS, GOOGLE_START_URL } from '../auth/GoogleSignIn';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function mockServer({ session, google }: { session: boolean; google: boolean }) {
  return vi.spyOn(window, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/auth/me')) {
      return session
        ? jsonResponse(200, { success: true, user: { username: 'asha@matrix.example', role: 'user', provider: 'google' } })
        : jsonResponse(401, { success: false });
    }
    if (url.includes('/api/auth/providers')) {
      return jsonResponse(200, { success: true, providers: { password: true, google } });
    }
    if (url.includes('/api/auth/login')) {
      return jsonResponse(200, { success: true, user: { username: 'alice', role: 'user' } });
    }
    return jsonResponse(404, { success: false });
  });
}

function renderApp() {
  return render(
    <AuthProvider>
      <RequireAuth>
        <div>transcript page</div>
      </RequireAuth>
    </AuthProvider>,
  );
}

describe('Continue with Google', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/transcript');
  });
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, '', '/');
  });

  it('renders the Google button next to the existing username/password form', async () => {
    mockServer({ session: false, google: true });
    renderApp();
    const button = await screen.findByRole('link', { name: 'Continue with Google' });
    expect(screen.getByRole('separator', { name: 'or' })).toBeTruthy();
    expect(screen.getByLabelText('Username')).toBeTruthy();
    expect(screen.getByLabelText('Password')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy();
    expect(button).toBeTruthy();
  });

  it('starts the server-side OAuth flow (no client-side token handling)', async () => {
    mockServer({ session: false, google: true });
    renderApp();
    const button = await screen.findByRole('link', { name: 'Continue with Google' });
    expect(button.getAttribute('href')).toBe(GOOGLE_START_URL);
    expect(GOOGLE_START_URL.endsWith('/api/auth/google/start')).toBe(true);
  });

  it('is hidden when the server has not enabled Google sign-in', async () => {
    const fetchMock = mockServer({ session: false, google: false });
    renderApp();
    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeTruthy();
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/api/auth/providers'))).toBe(true));
    expect(screen.queryByRole('link', { name: 'Continue with Google' })).toBeNull();
    expect(screen.getByLabelText('Username')).toBeTruthy();
  });

  it('password sign-in still works when Google is enabled', async () => {
    mockServer({ session: false, google: true });
    renderApp();
    await screen.findByRole('link', { name: 'Continue with Google' });
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.submit(screen.getByRole('form', { name: 'Sign in' }));
    expect(await screen.findByText('transcript page')).toBeTruthy();
  });

  it('after a successful Google sign-in the session opens the transcript page', async () => {
    // The backend set the session cookie and redirected to /transcript.
    mockServer({ session: true, google: true });
    renderApp();
    expect(await screen.findByText('transcript page')).toBeTruthy();
    expect(screen.queryByRole('form', { name: 'Sign in' })).toBeNull();
  });

  it.each(Object.entries(GOOGLE_AUTH_ERRORS))('shows a safe message for auth_error=%s', async (code, message) => {
    window.history.replaceState(null, '', `/transcript?auth_error=${code}`);
    mockServer({ session: false, google: true });
    renderApp();
    expect((await screen.findByRole('alert')).textContent).toBe(message);
    expect(window.location.search).toBe(''); // removed so a reload does not repeat it
  });

  it('never displays an unknown error value from the URL', async () => {
    window.history.replaceState(null, '', '/transcript?auth_error=%3Cscript%3Ealert(1)%3C%2Fscript%3E');
    mockServer({ session: false, google: true });
    renderApp();
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe(GOOGLE_AUTH_ERRORS.failed);
    expect(document.body.innerHTML).not.toContain('<script>alert');
  });
});
