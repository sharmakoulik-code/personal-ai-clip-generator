// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RenderPanel from './RenderPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const clips = [{ id:'c1', start:2, end:22, title:'First', selected:true }, { id:'c2', start:30, end:55, title:'Second', selected:true }];

describe('vertical rendering', () => {
  it('configures preview, framing and starts a selected clip render', async () => {
    const fetcher = vi.fn(async (path: RequestInfo | URL) => response(String(path).startsWith('/api/renders?') ? [] : { id:'r1' }, 202));
    vi.stubGlobal('fetch', fetcher);
    const user = userEvent.setup();
    render(<RenderPanel videoId="v1" projectId={1} clips={clips} />);
    expect((await screen.findByAltText('Representative crop preview')).getAttribute('src')).toContain('/api/clips/c1/render-preview');
    await user.selectOptions(screen.getByLabelText('Layout'), 'center');
    await user.selectOptions(screen.getByLabelText('Resolution'), '1280');
    await user.selectOptions(screen.getByLabelText('Quality'), 'small');
    fireEvent.change(screen.getByLabelText('Horizontal framing'), { target: { value: '0.5' } });
    await user.click(screen.getByRole('button', {name:'Render selected clip'}));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/clips/c1/render', expect.objectContaining({ body: expect.stringContaining('"layout":"center"') })));
    const sent = fetcher.mock.calls.find(c => String(c[0]).includes('/render'));
    expect(sent).toBeTruthy();
    await user.click(screen.getByRole('button', {name:'Reset to automatic tracking'}));
    expect((screen.getByLabelText('Layout') as HTMLSelectElement).value).toBe('smart');
  });

  it('renders batch, shows progress and downloadable video', async () => {
    const rows = [{id:'r1',batch_id:'b1',project_id:1,video_id:'v1',clip_title:'First',status:'completed',stage:'Render completed',progress:100,error:null,tracking_status:'face_framing',width:720,height:1280},{id:'r2',batch_id:'b1',project_id:1,video_id:'v1',clip_title:'Second',status:'running',stage:'Rendering video',progress:40,error:null,tracking_status:null,width:720,height:1280}];
    const fetcher = vi.fn(async (path: RequestInfo | URL) => response(String(path).startsWith('/api/renders?') ? rows : { id:'r3' }));
    vi.stubGlobal('fetch', fetcher);
    const user = userEvent.setup();
    render(<RenderPanel videoId="v1" projectId={1} clips={clips} />);
    expect(await screen.findByText('Latest batch: 1/2 completed')).toBeTruthy();
    expect(screen.getByText('70%')).toBeTruthy();
    expect(screen.getByRole('link', { name:'Download MP4' }).getAttribute('href')).toBe('/api/renders/r1/download');
    await user.click(screen.getByRole('button', { name:'Render all selected clips in project' }));
    expect(fetcher).toHaveBeenCalledWith('/api/projects/1/render-batch', expect.objectContaining({method:'POST'}));
    await user.click(screen.getByRole('button', { name:'Cancel render' }));
    expect(fetcher).toHaveBeenCalledWith('/api/renders/r2/cancel', {method:'POST'});
  });
});

describe('subject framing and captions', () => {
  it('submits subject, speaker and caption options and shows failure and SRT download', async () => {
    const rows = [
      {id:'ok',clip_id:'c1',batch_id:'b1',project_id:1,video_id:'v1',clip_title:'First',status:'completed',stage:'Render completed',progress:100,error:null,tracking_status:'haar_face_framing',width:720,height:1280},
      {id:'bad',clip_id:'c2',batch_id:'b1',project_id:1,video_id:'v1',clip_title:'Second',status:'failed',stage:'Failed',progress:20,error:'No timed transcript words',tracking_status:null,width:720,height:1280},
    ];
    const fetcher=vi.fn(async (path: RequestInfo | URL) => response(String(path).startsWith('/api/renders?') ? rows : {id:'new'},202));
    vi.stubGlobal('fetch',fetcher);
    const user=userEvent.setup();
    render(<RenderPanel videoId="v1" projectId={1} clips={clips} initialClipId="c2" />);
    expect(await screen.findByText(/No timed transcript words/)).toBeTruthy();
    expect(screen.getByRole('link',{name:'Download SRT'}).getAttribute('href')).toBe('/api/clips/c1/captions.srt');
    await user.selectOptions(screen.getByLabelText('Tracking engine'),'subject');
    expect((screen.getByLabelText('Follow active speaker (heuristic)') as HTMLInputElement).checked).toBe(true);
    fireEvent.change(screen.getByLabelText('Camera smoothness'),{target:{value:'1'}});
    await user.click(screen.getByLabelText('Burn in captions'));
    await user.selectOptions(screen.getByLabelText('Caption style'),'classic');
    await user.click(screen.getByLabelText('Uppercase captions'));
    await user.click(screen.getByRole('button',{name:'Render selected clip'}));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/clips/c2/render',expect.objectContaining({body:expect.stringContaining('"caption_style":"classic"')})));
    const sent=fetcher.mock.calls.find(call => String(call[0])==='/api/clips/c2/render') as unknown as [unknown, RequestInit];
    expect(JSON.parse(String(sent[1].body))).toMatchObject({tracking_engine:'subject',smooth_time:1,active_speaker:true,captions:true,caption_uppercase:true});
  });
});
