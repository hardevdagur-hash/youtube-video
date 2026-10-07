import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { AuthProvider, RequireAuth, installAuthInterceptor } from '../auth/AuthContext';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

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
    vi.spyOn(window, 'fetch').mockResolvedValue(jsonResponse(401, { success: false }));
    renderGate();
    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeTruthy();
    expect(screen.queryByText('secret tool page')).toBeNull();
  });

  it('renders the page for an existing session', async () => {
    vi.spyOn(window, 'fetch').mockResolvedValue(
      jsonResponse(200, { success: true, user: { username: 'alice', role: 'user' } }),
    );
    renderGate();
    expect(await screen.findByText('secret tool page')).toBeTruthy();
  });

  it('signs in and then renders the page', async () => {
    const fetchMock = vi.spyOn(window, 'fetch');
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { success: false }));
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, { success: true, user: { username: 'alice', role: 'user' } }),
    );
    renderGate();
    fireEvent.change(await screen.findByLabelText('Username'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.submit(screen.getByRole('form', { name: 'Sign in' }));
    expect(await screen.findByText('secret tool page')).toBeTruthy();
    const [url, init] = fetchMock.mock.calls[1];
    expect(String(url)).toContain('/api/auth/login');
    expect(init?.method).toBe('POST');
  });

  it('shows the server error for bad credentials', async () => {
    const fetchMock = vi.spyOn(window, 'fetch');
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { success: false }));
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { success: false, error: 'Invalid username or password.' }));
    renderGate();
    fireEvent.change(await screen.findByLabelText('Username'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'nope' } });
    fireEvent.submit(screen.getByRole('form', { name: 'Sign in' }));
    expect((await screen.findByRole('alert')).textContent).toBe('Invalid username or password.');
  });

  it('returns to the sign-in form when an API call reports 401', async () => {
    const fetchMock = vi.spyOn(window, 'fetch');
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, { success: true, user: { username: 'alice', role: 'user' } }),
    );
    installAuthInterceptor();
    renderGate();
    expect(await screen.findByText('secret tool page')).toBeTruthy();

    fetchMock.mockResolvedValueOnce(jsonResponse(401, { success: false }));
    await act(async () => {
      await window.fetch('/api/transcript/jobs/0123456789ab');
    });
    await waitFor(() => expect(screen.queryByText('secret tool page')).toBeNull());
    expect(screen.getByRole('form', { name: 'Sign in' })).toBeTruthy();
  });
});
