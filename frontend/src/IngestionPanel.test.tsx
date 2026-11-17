// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import IngestionPanel from './IngestionPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const project = { id: 1, name: 'Project', created_at: '2026-01-01T00:00:00Z' };

describe('ingestion panel', () => {
  it('fetches and previews YouTube metadata before confirming download', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/videos/youtube/metadata') return response({ video_id: 'abcdefghijk', url: 'https://www.youtube.com/watch?v=abcdefghijk', title: 'Test video', channel: 'Test channel', duration: 75, thumbnail: null, formats: [{ height: 720, ext: 'mp4', filesize: 5000 }] });
      if (path === '/api/videos/youtube/download' && init?.method === 'POST') return response({ id: 'job1' }, 202);
      return response([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<IngestionPanel projects={[project]} selected={1} onSelect={() => {}} online />);
    await user.type(screen.getByLabelText('YouTube URL'), 'https://youtu.be/abcdefghijk');
    await user.click(screen.getByRole('button', { name: 'Fetch information' }));
    expect(await screen.findByText('Test video')).toBeTruthy();
    expect(screen.getByText(/Test channel/)).toBeTruthy();
    await user.selectOptions(screen.getByLabelText('Download quality'), '720');
    await user.click(screen.getByRole('button', { name: 'Confirm download' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/videos/youtube/download', expect.objectContaining({ body: expect.stringContaining('"quality":"720"') })));
  });

  it('shows job progress, a video library, and requires delete confirmation', async () => {
    const job = { id: 'j1', project_id: 1, source: 'example.mp4', kind: 'upload', status: 'uploading', progress: 35, speed: null, error: null };
    const video = { id: 'v1', title: 'Saved video', source_type: 'upload', duration: 60, size_bytes: 4096, width: 160, height: 120, has_audio: true, has_thumbnail: false };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/videos/v1' && init?.method === 'DELETE') return new Response(null, { status: 204 });
      return response(path === '/api/jobs' ? [job] : [video]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const user = userEvent.setup();
    render(<IngestionPanel projects={[project]} selected={1} onSelect={() => {}} online />);
    expect(await screen.findByText('Saved video')).toBeTruthy();
    expect(screen.getByText(/35%/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Delete video Saved video' }));
    expect(confirm).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalledWith('/api/videos/v1', expect.anything());
    confirm.mockReturnValue(true);
    await user.click(screen.getByRole('button', { name: 'Delete video Saved video' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/videos/v1', { method: 'DELETE' }));
  });

  it('starts a local upload, displays real progress, and can cancel it', async () => {
    let request: FakeXHR | undefined;
    class FakeXHR {
      upload: { onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
      onload: (() => void) | null = null; onerror: (() => void) | null = null; onabort: (() => void) | null = null;
      status = 202; responseText = ''; body: unknown; aborted = false;
      constructor() { request = this; }
      open() {} setRequestHeader() {} send(body: unknown) { this.body = body; }
      abort() { this.aborted = true; this.onabort?.(); }
    }
    let started = false;
    const job = { id: 'up1', project_id: 1, source: 'clip.mp4', kind: 'upload', status: 'uploading', progress: 20, speed: null, error: null };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/videos/upload' && init?.method === 'POST') { started = true; return response(job, 201); }
      if (path === '/api/jobs/up1/cancel') return response(job);
      return response(path === '/api/jobs' && started ? [job] : []);
    });
    vi.stubGlobal('fetch', fetchMock); vi.stubGlobal('XMLHttpRequest', FakeXHR);
    const user = userEvent.setup();
    render(<IngestionPanel projects={[project]} selected={1} onSelect={() => {}} online />);
    const file = new File(['video'], 'clip.mp4', { type: 'video/mp4' });
    await user.upload(screen.getByLabelText('Upload a video'), file);
    await waitFor(() => expect(request?.body).toBe(file));
    act(() => request?.upload.onprogress?.({ lengthComputable: true, loaded: 2, total: 5 }));
    expect(screen.getByText('Uploading 40%')).toBeTruthy();
    await user.click(await screen.findByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(request?.aborted).toBe(true));
    expect(fetchMock).toHaveBeenCalledWith('/api/jobs/up1/cancel', { method: 'POST' });
  });

  it.each(['error', 'http'] as const)('does not report a %s failure when cancellation closes the upload first', async (outcome) => {
    let request: FakeXHR | undefined;
    let resolveCancel: ((value: Response) => void) | undefined;
    class FakeXHR {
      upload = { onprogress: null };
      onload: (() => void) | null = null; onerror: (() => void) | null = null; onabort: (() => void) | null = null;
      status = 0; responseText = ''; body: unknown;
      constructor() { request = this; }
      open() {} setRequestHeader() {} send(body: unknown) { this.body = body; }
      abort() { this.onabort?.(); }
    }
    let started = false;
    const job = { id: 'race', project_id: 1, source: 'test.mp4', kind: 'upload', status: 'uploading', progress: 10, speed: null, error: null };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/videos/upload' && init?.method === 'POST') { started = true; return response(job, 201); }
      if (path === '/api/jobs/race/cancel') return new Promise<Response>(resolve => { resolveCancel = resolve; });
      return response(path === '/api/jobs' && started ? [job] : []);
    });
    vi.stubGlobal('fetch', fetchMock); vi.stubGlobal('XMLHttpRequest', FakeXHR);
    const user = userEvent.setup();
    render(<IngestionPanel projects={[project]} selected={1} onSelect={() => {}} online />);
    await user.upload(screen.getByLabelText('Upload a video'), new File(['video'], 'test.mp4', { type: 'video/mp4' }));
    await waitFor(() => expect(request?.body).toBeTruthy());
    await user.click(await screen.findByRole('button', { name: 'Cancel' }));
    // Backend closes the stream before the cancellation response arrives.
    act(() => {
      if (outcome === 'error') request?.onerror?.();
      else if (request) { request.status = 409; request.responseText = JSON.stringify({ detail: 'Upload cancelled' }); request.onload?.(); }
    });
    expect(screen.queryByRole('alert')).toBeNull();
    await act(async () => { resolveCancel?.(response({ ...job, status: 'cancelled' })); });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('still reports an unrelated upload network error', async () => {
    let request: FakeXHR | undefined;
    class FakeXHR {
      upload = { onprogress: null }; onload: (() => void) | null = null; onerror: (() => void) | null = null; onabort: (() => void) | null = null;
      status = 0; responseText = ''; body: unknown;
      constructor() { request = this; }
      open() {} setRequestHeader() {} send(body: unknown) { this.body = body; } abort() { this.onabort?.(); }
    }
    const job = { id: 'network', project_id: 1, source: 'test.mp4', kind: 'upload', status: 'uploading', progress: null, speed: null, error: null };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input) === '/api/videos/upload' ? response(job, 201) : response([])));
    vi.stubGlobal('XMLHttpRequest', FakeXHR);
    const user = userEvent.setup();
    render(<IngestionPanel projects={[project]} selected={1} onSelect={() => {}} online />);
    await user.upload(screen.getByLabelText('Upload a video'), new File(['video'], 'test.mp4', { type: 'video/mp4' }));
    await waitFor(() => expect(request?.body).toBeTruthy());
    act(() => request?.onerror?.());
    expect((await screen.findByRole('alert')).textContent).toContain('Upload failed. Check your connection.');
  });

  it('rejects an unsupported local file before uploading', async () => {
    const fetchMock = vi.fn(async () => response([]));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup({ applyAccept: false });
    render(<IngestionPanel projects={[project]} selected={1} onSelect={() => {}} online />);
    await user.upload(screen.getByLabelText('Upload a video'), new File(['hello'], 'bad.exe', { type: 'application/octet-stream' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Choose a non-empty');
    expect(fetchMock).not.toHaveBeenCalledWith('/api/videos/upload', expect.anything());
  });
});
