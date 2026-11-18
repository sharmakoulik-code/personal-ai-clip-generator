// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AnalysisPanel from './AnalysisPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const makeClip = (id: string, title: string, score: number, start: number, end: number) => ({ id, title, start, end, score, selected: false, hook: title, transcript: `${title} Full transcript.`, reason: 'Question and context.', scores: { hook_strength: 70 } });

describe('AI analysis', () => {
  it('starts analysis using selected settings and displays actual job stage and progress', async () => {
    let job: object[] = [];
    const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/analyze') && init?.method === 'POST') { job = [{ id: 'j1', status: 'running', stage: 'Transcribing video', progress: 37, error: null }]; return response(job[0], 202); }
      if (path.endsWith('/analysis-jobs')) return response(job);
      if (path.endsWith('/transcription')) return response({ detail: 'No transcription available' }, 404);
      return response([]);
    });
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    render(<AnalysisPanel videoId="v1" hasAudio />);
    await user.selectOptions(screen.getByLabelText('Whisper model'), 'tiny');
    await user.selectOptions(screen.getByLabelText('Clip duration'), '15-30');
    await user.click(screen.getByRole('button', { name: 'Analyze Video with AI' }));
    expect(await screen.findByText('Transcribing video')).toBeTruthy();
    expect(screen.getByText('37% of source audio')).toBeTruthy();
    expect(mock).toHaveBeenCalledWith('/api/videos/v1/analyze', expect.objectContaining({ body: JSON.stringify({ model: 'tiny', duration_range: '15-30', force_transcribe: false, cloud_titles: false }) }));
    expect((screen.getByRole('button', { name: 'Analyze Video with AI' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('displays, sorts, opens transcripts, and saves individual and all selections', async () => {
    let clips = [makeClip('a', 'Short clip', 90, 2, 20), makeClip('b', 'Long clip', 70, 30, 59)];
    const transcript = { model: 'base', language: 'en', segments: [{ start: 2, end: 20, text: 'Short clip. Full transcript.' }] };
    const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/clips/a' && init?.method === 'PATCH') { clips = clips.map(c => c.id === 'a' ? { ...c, selected: JSON.parse(String(init.body)).selected } : c); return response(clips[0]); }
      if (path.endsWith('/clips/selection')) { clips = clips.map(c => ({ ...c, selected: JSON.parse(String(init?.body)).selected })); return response(clips); }
      if (path.endsWith('/analysis-jobs')) return response([{ id: 'j', status: 'completed', stage: 'Analysis completed', progress: 100, error: null }]);
      if (path.endsWith('/transcription')) return response(transcript);
      return response(clips);
    });
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    render(<AnalysisPanel videoId="v1" hasAudio />);
    expect(await screen.findByText('Suggested clips (2)')).toBeTruthy();
    const checkboxes = screen.getAllByRole('checkbox').filter(x => x.getAttribute('aria-label')?.startsWith('Select clip'));
    await user.click(checkboxes[0]);
    await waitFor(() => expect((screen.getByLabelText('Select clip 1') as HTMLInputElement).checked).toBe(true));
    await user.click(screen.getByRole('button', { name: 'Select all' }));
    await waitFor(() => expect((screen.getByLabelText('Select clip 2') as HTMLInputElement).checked).toBe(true));
    await user.click(screen.getByRole('button', { name: 'Deselect all' }));
    await waitFor(() => expect((screen.getByLabelText('Select clip 1') as HTMLInputElement).checked).toBe(false));
    await user.selectOptions(screen.getByLabelText('Sort by'), 'duration');
    const cards = screen.getAllByRole('article');
    expect(cards[0].querySelector('strong')?.textContent).toContain('Long clip');
    await user.click(screen.getByText(/View full transcript/));
    expect(screen.getByText('Short clip. Full transcript.')).toBeTruthy();
    expect(mock).toHaveBeenCalledWith('/api/videos/v1/clips/selection', expect.objectContaining({ method: 'PATCH' }));
  });

  it('opts in to Cloud AI and discloses fallback and privacy', async () => {
    const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/analyze') && init?.method === 'POST') return response({ id: 'j1' }, 202);
      if (path.endsWith('/transcription')) return response({}, 404);
      return response([]);
    });
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    render(<AnalysisPanel videoId="v1" hasAudio />);
    await user.selectOptions(screen.getByLabelText('Title and explanation provider'), 'cloud');
    expect(screen.getByText(/tries Groq first, then Google Gemini/)).toBeTruthy();
    expect(screen.getByText(/may be sent to both/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Analyze Video with AI' }));
    expect(mock).toHaveBeenCalledWith('/api/videos/v1/analyze', expect.objectContaining({ body: JSON.stringify({ model: 'base', duration_range: '30-45', force_transcribe: false, cloud_titles: true }) }));
  });

  it('cancels a running analysis', async () => {
    let job = { id: 'j1', status: 'running', stage: 'Transcribing video', progress: 25, error: null as string | null };
    const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/cancel') && init?.method === 'POST') { job = { ...job, status: 'cancelled', stage: 'Cancelled', error: 'Analysis cancelled.' }; return response(job); }
      if (path.endsWith('/analysis-jobs')) return response([job]);
      if (path.endsWith('/transcription')) return response({}, 404);
      return response([]);
    });
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    render(<AnalysisPanel videoId="v1" hasAudio />);
    await user.click(await screen.findByRole('button', { name: 'Cancel analysis' }));
    expect(await screen.findByText('Cancelled')).toBeTruthy();
    expect(mock).toHaveBeenCalledWith('/api/analysis/jobs/j1/cancel', { method: 'POST' });
  });

  it('disables analysis when a video has no audio', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input).endsWith('/transcription') ? response({}, 404) : response([])));
    render(<AnalysisPanel videoId="v1" hasAudio={false} />);
    expect(screen.getByText(/no audio stream/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Analyze Video with AI' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('AI-picked clip selection', () => {
  it('submits the opt-in mode, explains privacy, and shows AI score breakdown', async () => {
    const clip = {...makeClip('a','AI pick',90,0,20), reason:'Groq selection (review): Interesting.', scores:{llm_hook:80,llm_payoff:90}};
    const fetcher=vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path=String(input);
      if (path.endsWith('/analyze') && init?.method==='POST') return response({id:'j'},202);
      if (path.endsWith('/analysis-jobs')) return response([{id:'j',status:'completed',stage:'Analysis completed',progress:100,error:null}]);
      if (path.endsWith('/transcription')) return response({model:'base',language:'en',segments:[]});
      if (path.endsWith('/clips')) return response([clip]);
      return response([]);
    });
    vi.stubGlobal('fetch',fetcher);
    const user=userEvent.setup();
    render(<AnalysisPanel videoId="v1" hasAudio projectId={1} />);
    expect(await screen.findByText('Suggested clips (1)')).toBeTruthy();
    await user.selectOptions(screen.getByLabelText('Clip selection'),'llm');
    expect(screen.getByText(/numbered transcript excerpts and timings/)).toBeTruthy();
    await user.click(screen.getByRole('button',{name:'Run analysis again'}));
    expect(fetcher).toHaveBeenCalledWith('/api/videos/v1/analyze',expect.objectContaining({body:expect.stringContaining('"selection_mode":"llm"')}));
    await user.click(screen.getByText('View clip transcript and scores'));
    expect(screen.getByText('llm hook: 80/100')).toBeTruthy();
    await user.click(screen.getByRole('button',{name:'Render vertical clip'}));
    expect((screen.getByLabelText('Clip to preview') as HTMLSelectElement).value).toBe('a');
  });
});
