// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const jsonResponse = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });

describe('dashboard', () => {
  it('loads, creates and deletes a project through the API', async () => {
    const project = { id: 1, name: 'My video', created_at: '2026-01-01T00:00:00Z' };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/health') return jsonResponse({ status: 'ok' });
      if (path === '/api/projects' && init?.method === 'POST') return jsonResponse(project, 201);
      if (path === '/api/projects/1' && init?.method === 'DELETE') return new Response(null, { status: 204 });
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText('No projects just yet');
    expect((screen.getByLabelText('YouTube URL') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText('Upload a video') as HTMLInputElement).disabled).toBe(true);
    await user.click(screen.getByRole('button', { name: 'Create your first project' }));
    await user.type(screen.getByLabelText('Project name'), 'My video');
    await user.click(screen.getByRole('button', { name: 'Create project' }));
    expect(await screen.findByText('My video')).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith('/api/projects', expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'My video' }) }));
    await user.click(screen.getByRole('button', { name: 'Delete My video' }));
    await screen.findByText('No projects just yet');
    expect(fetchMock).toHaveBeenCalledWith('/api/projects/1', expect.objectContaining({ method: 'DELETE' }));
  });

  it('reports a backend connection failure and allows retry', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('offline'));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText('Backend offline')).toBeTruthy();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => jsonResponse(String(input) === '/api/projects' ? [] : { status: 'ok' }));
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByText('Local workspace')).toBeTruthy());
  });
});
