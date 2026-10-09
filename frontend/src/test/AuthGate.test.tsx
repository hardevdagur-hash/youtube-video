import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { AuthProvider, RequireAuth, installAuthInterceptor } from '../auth/AuthContext';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/**
 * Mock fetch by URL: each route answers with its queued responses in order (the last one
 * repeats). Routing by URL keeps these tests independent of how many other requests the
 * sign-in page makes (e.g. /api/auth/providers).
 */
function mockApi(routes: Record<string, Response[]>) {
  const queues = Object.fromEntries(Object.entries(routes).map(([k, v]) => [k, [...v]]));
  return vi.spyOn(window, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const key = Object.keys(queues).find((path) => url.includes(path));
    if (!key) return jsonResponse(404, { success: false });
    const queue = queues[key];
    const next = queue.length > 1 ? queue.shift()! : queue[0];
    return next.clone();
  });
}

const callsTo = (fetchMock: ReturnType<typeof mockApi>, path: string) =>
  fetchMock.mock.calls.filter(([input]) => String(input).includes(path));

function renderGate() {
  return render(
    <AuthProvider>
      <RequireAuth>
        <div>secret tool page</div>
      </RequireAuth>
    </AuthProvider>,
  );
}

describe('RequireAuth', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the sign-in form when there is no session', async () => {
    mockApi({ '/api/auth/me': [jsonResponse(401, { success: false })] });
    renderGate();
    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeTruthy();
    expect(screen.queryByText('secret tool page')).toBeNull();
  });

  it('renders the page for an existing session', async () => {
    mockApi({ '/api/auth/me': [jsonResponse(200, { success: true, user: { username: 'alice', role: 'user' } })] });
    renderGate();
    expect(await screen.findByText('secret tool page')).toBeTruthy();
  });

  it('signs in and then renders the page', async () => {
    const fetchMock = mockApi({
      '/api/auth/me': [jsonResponse(401, { success: false })],
      '/api/auth/login': [jsonResponse(200, { success: true, user: { username: 'alice', role: 'user' } })],
    });
    renderGate();
    fireEvent.change(await screen.findByLabelText('Username'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.submit(screen.getByRole('form', { name: 'Sign in' }));
    expect(await screen.findByText('secret tool page')).toBeTruthy();
    const [loginCall] = callsTo(fetchMock, '/api/auth/login');
    expect(loginCall).toBeTruthy();
    expect(loginCall[1]?.method).toBe('POST');
  });

  it('shows the server error for bad credentials', async () => {
    mockApi({
      '/api/auth/me': [jsonResponse(401, { success: false })],
      '/api/auth/login': [jsonResponse(401, { success: false, error: 'Invalid username or password.' })],
    });
    renderGate();
    fireEvent.change(await screen.findByLabelText('Username'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'nope' } });
    fireEvent.submit(screen.getByRole('form', { name: 'Sign in' }));
    expect((await screen.findByRole('alert')).textContent).toBe('Invalid username or password.');
  });

  it('returns to the sign-in form when an API call reports 401', async () => {
    mockApi({
      '/api/auth/me': [jsonResponse(200, { success: true, user: { username: 'alice', role: 'user' } })],
      '/api/transcript/jobs/': [jsonResponse(401, { success: false })],
    });
    installAuthInterceptor();
    renderGate();
    expect(await screen.findByText('secret tool page')).toBeTruthy();

    await act(async () => {
      await window.fetch('/api/transcript/jobs/0123456789ab');
    });
    await waitFor(() => expect(screen.queryByText('secret tool page')).toBeNull());
    expect(screen.getByRole('form', { name: 'Sign in' })).toBeTruthy();
  });
});
