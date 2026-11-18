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

describe('Shorts layout options', () => {
  it('defaults to auto and submits the optional stack control', async () => {
    const fetcher=vi.fn(async (path: RequestInfo | URL) => response(String(path).startsWith('/api/renders?') ? [] : {id:'new'},202));
    vi.stubGlobal('fetch',fetcher);
    const user=userEvent.setup();
    render(<RenderPanel videoId="v1" projectId={1} clips={clips} />);
    expect((screen.getByLabelText('Layout') as HTMLSelectElement).value).toBe('auto');
    await user.click(screen.getByLabelText('Stack two speakers when possible'));
    await user.click(screen.getByRole('button',{name:'Render selected clip'}));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/clips/c1/render',expect.objectContaining({body:expect.stringContaining('"stack_two_speakers":true')})));
  });
});

it('exposes the caption override and render-specific subtitle links', async () => {
  const rows=[{id:'r1',clip_id:'c1',batch_id:'b1',project_id:1,video_id:'v1',clip_title:'First',status:'completed',stage:'done',progress:100,error:null,tracking_status:null,width:1080,height:1920,settings:{preset:'shorts'}}];
  const fetcher=vi.fn(async (path: RequestInfo | URL) => response(String(path).startsWith('/api/renders?') ? rows : {id:'new'},202));
  vi.stubGlobal('fetch',fetcher);
  const user=userEvent.setup();
  render(<RenderPanel videoId="v1" projectId={1} clips={clips} />);
  expect((await screen.findByRole('link',{name:'Download SRT'})).getAttribute('href')).toBe('/api/clips/c1/captions.srt?render_id=r1');
  expect(screen.getByRole('link',{name:'Download VTT'}).getAttribute('href')).toBe('/api/clips/c1/captions.vtt?render_id=r1');
  await user.click(screen.getByLabelText('Burn in captions'));
  await user.click(screen.getByLabelText('Add captions anyway'));
  await user.click(screen.getByRole('button',{name:'Render selected clip'}));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/clips/c1/render',expect.objectContaining({body:expect.stringContaining('"force_captions":true')})));
});

describe('Shorts workflow', () => {
  it('applies preset defaults, displays warnings, and submits the override', async () => {
    const rows: unknown[]=[];
    const fetcher=vi.fn(async (path:RequestInfo|URL)=>{
      const url=String(path);
      if(url.endsWith('/preflight'))return response({warnings:[{code:'possible_burned_in_captions',severity:'warn',message:'Possible captions detected.'},{code:'too_short',severity:'info',message:'Source is short.'}]});
      if(url.startsWith('/api/renders?'))return response(rows);
      return response({id:'new'},202);
    });
    vi.stubGlobal('fetch',fetcher);
    const user=userEvent.setup();
    render(<RenderPanel shortsMode videoId="v1" projectId={1} clips={clips}/>);
    expect((screen.getByLabelText('YouTube Shorts preset') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('Burn in captions') as HTMLInputElement).checked).toBe(true);
    expect(await screen.findByText(/Possible captions detected/)).toBeTruthy();
    expect(screen.getByText(/Source is short/)).toBeTruthy();
    await user.click(screen.getByLabelText('Add captions anyway'));
    await user.click(screen.getByRole('button',{name:'Render selected clip'}));
    const sent=fetcher.mock.calls.find(call=>String(call[0])==='/api/clips/c1/render') as unknown as [unknown,RequestInit];
    expect(JSON.parse(String(sent[1].body))).toMatchObject({preset:'shorts',layout:'auto',captions:true,caption_safe_zone:true,tighten:true,loudness_lufs:-14,force_captions:true});
  });
  it('generates metadata and shows a session notice once',async()=>{
    sessionStorage.clear();
    const rows=[{id:'r1',clip_id:'c1',batch_id:'b',project_id:1,video_id:'v1',clip_title:'First',status:'completed',stage:'done',progress:100,error:null,tracking_status:null,width:1080,height:1920,settings:{preset:'shorts'},info:{duration:30,layout_used:'follow',removed_seconds:1,loudness:{before:-20,after:-14}}}];
    const fetcher=vi.fn(async (path:RequestInfo|URL,init?:RequestInit)=>{
      const url=String(path);
      if(url.startsWith('/api/renders?'))return response(rows);
      if(url.endsWith('/metadata') && init?.method==='POST')return response({titles:['A title'],description:'Description',hashtags:['#test'],provider:'basic (no AI)'});
      if(url.endsWith('/metadata'))return response({},404);
      return response({});
    });
    vi.stubGlobal('fetch',fetcher);
    const user=userEvent.setup();
    render(<RenderPanel shortsMode videoId="v1" projectId={1} clips={clips}/>);
    await user.click(await screen.findByRole('button',{name:'Generate metadata'}));
    expect(await screen.findByText(/Provider: basic/)).toBeTruthy();
    expect(screen.getByRole('button',{name:'Copy title 1'})).toBeTruthy();
    const link=screen.getByRole('link',{name:'Download MP4'}); link.addEventListener('click',e=>e.preventDefault());
    await user.click(link);
    expect(screen.getByText(/If your video contains music or footage/)).toBeTruthy();
    await user.click(screen.getByRole('button',{name:'Got it'}));
    await user.click(link);
    expect(screen.queryByText(/If your video contains music or footage/)).toBeNull();
  });
});

it('blocks only captions when preflight reports no audio',async()=>{
  const fetcher=vi.fn(async (path:RequestInfo|URL)=>{
    const url=String(path);
    if(url.endsWith('/preflight'))return response({warnings:[{code:'no_audio',severity:'block',message:'No audio stream.'}]});
    if(url.startsWith('/api/renders?'))return response([]);
    return response({});
  });
  vi.stubGlobal('fetch',fetcher);
  render(<RenderPanel shortsMode videoId="v1" projectId={1} clips={clips}/>);
  expect(await screen.findByText(/No audio stream/)).toBeTruthy();
  expect((screen.getByLabelText('Burn in captions') as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole('button',{name:'Render selected clip'}) as HTMLButtonElement).disabled).toBe(false);
});

it('configures cinematic bars and validates colour before submitting',async()=>{
  const fetcher=vi.fn(async(path:RequestInfo|URL)=>response(String(path).startsWith('/api/renders?')?[]:{id:'new'},202));
  vi.stubGlobal('fetch',fetcher);
  const user=userEvent.setup();
  render(<RenderPanel videoId="v1" projectId={1} clips={clips}/>);
  expect(screen.getByRole('option',{name:'Cinematic bars'})).toBeTruthy();
  await user.selectOptions(screen.getByLabelText('Layout'),'letterbox');
  expect(screen.getByLabelText('Cinematic bars schematic')).toBeTruthy();
  expect((screen.getByLabelText('Caption position') as HTMLSelectElement).value).toBe('on_video');
  const colour=screen.getByLabelText('Background colour');
  await user.clear(colour); await user.type(colour,'red');
  expect(screen.getByRole('alert').textContent).toContain('six-digit hex');
  expect((screen.getByRole('button',{name:'Render selected clip'}) as HTMLButtonElement).disabled).toBe(true);
  await user.clear(colour); await user.type(colour,'#1a2B3c');
  fireEvent.change(screen.getByLabelText('Vertical position'),{target:{value:'.1'}});
  await user.click(screen.getByLabelText('Soft video edge'));
  await user.click(screen.getByRole('button',{name:'Render selected clip'}));
  await waitFor(()=>expect(fetcher).toHaveBeenCalledWith('/api/clips/c1/render',expect.objectContaining({method:'POST'})));
  const call=fetcher.mock.calls.find(c=>String(c[0])==='/api/clips/c1/render') as unknown as [unknown,RequestInit];
  expect(JSON.parse(String(call[1].body))).toMatchObject({layout:'letterbox',letterbox_bg:'#1a2B3c',letterbox_vertical_bias:.1,letterbox_edge:true,caption_position:'on_video'});
});
