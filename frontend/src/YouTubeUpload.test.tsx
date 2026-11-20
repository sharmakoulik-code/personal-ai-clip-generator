// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach, expect, it, vi} from 'vitest';
import YouTubeUpload from './YouTubeUpload';

afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.restoreAllMocks();});

it('only offers finished renders and uploads reviewed metadata and individual schedules after confirmation', async()=>{
  const requests: {url:string; init?:RequestInit}[] = [];
  const fetcher=vi.fn(async (input:RequestInfo|URL,init?:RequestInit)=>{
    const url=String(input); requests.push({url,init});
    if(url.includes('/status')) return Response.json({configured:true,connected:true,connecting:false});
    if(url.includes('/uploads')) return Response.json([]);
    if(url.includes('/metadata')) return Response.json({titles:['Suggested title'],description:'Suggested description'});
    if(url.includes('/upload')) return Response.json({status:'uploaded',video_id:'abcDEF12345'});
    return new Response('{}',{status:404});
  });
  vi.stubGlobal('fetch',fetcher);vi.spyOn(window,'confirm').mockReturnValue(true);
  const user=userEvent.setup();
  render(<YouTubeUpload projectId={1} renders={[{id:'a',clip_id:'c1',clip_title:'Ready clip',status:'completed'},{id:'b',clip_id:'c2',clip_title:'Unfinished clip',status:'running'}]}/>);
  await screen.findByText('Account: Connected for this app session');
  expect(screen.queryByText('Unfinished clip')).toBeNull();
  await user.click(screen.getByRole('checkbox',{name:'Ready clip'}));
  await waitFor(()=>expect(screen.getByLabelText('Title')).toHaveValue('Suggested title'));
  expect(screen.getByLabelText('Description')).toHaveValue('Suggested description');
  fireEvent.change(screen.getByLabelText('Publish publicly at (local time)'),{target:{value:'2099-01-01T10:00'}});
  await user.selectOptions(screen.getByLabelText('Made for kids?'),'no');
  await user.click(screen.getByRole('button',{name:'Upload and schedule selected'}));
  await screen.findByText(/1 video\(s\) uploaded/);
  const request=requests.find(r=>r.url==='/api/youtube/upload');
  const body=JSON.parse(request!.init!.body as string);
  expect(body).toEqual({render_id:'a',title:'Suggested title',description:'Suggested description',publish_at:new Date('2099-01-01T10:00').toISOString(),made_for_kids:false,retry_acknowledged:false});
  expect(window.confirm).toHaveBeenCalledOnce();
});

it('does not upload without an audience and future schedule',async()=>{
  const fetcher=vi.fn(async (input:RequestInfo|URL)=>String(input).includes('/status') ? Response.json({configured:true,connected:true,connecting:false}) : String(input).includes('/uploads') ? Response.json([]) : new Response('{}',{status:404}));
  vi.stubGlobal('fetch',fetcher); const user=userEvent.setup();
  render(<YouTubeUpload projectId={1} renders={[{id:'a',clip_id:'c1',clip_title:'Ready clip',status:'completed'}]}/>);
  await screen.findByText('Account: Connected for this app session');
  await user.click(screen.getByRole('checkbox',{name:'Ready clip'}));
  await user.click(screen.getByRole('button',{name:'Upload and schedule selected'}));
  expect(screen.getByRole('alert')).toHaveTextContent('future publication time');
  expect(fetcher.mock.calls.some(([url])=>String(url)==='/api/youtube/upload')).toBe(false);
});

it('uploads multiple selected renders sequentially with their own titles and schedules',async()=>{
  const sent: Array<{render_id:string;title:string;publish_at:string}> = [];
  const fetcher=vi.fn(async (input:RequestInfo|URL,init?:RequestInit)=>{
    const url=String(input);
    if(url.includes('/status')) return Response.json({configured:true,connected:true,connecting:false});
    if(url.includes('/uploads')) return Response.json([]);
    if(url.includes('/metadata')) return new Response('{}',{status:404});
    if(url==='/api/youtube/upload') {sent.push(JSON.parse(init!.body as string));return Response.json({status:'uploaded'});}
    return new Response('{}',{status:404});
  });
  vi.stubGlobal('fetch',fetcher);vi.spyOn(window,'confirm').mockReturnValue(true);
  const user=userEvent.setup();
  render(<YouTubeUpload projectId={1} renders={[{id:'a',clip_id:'c1',clip_title:'First',status:'completed'},{id:'b',clip_id:'c2',clip_title:'Second',status:'completed'}]}/>);
  await screen.findByText('Account: Connected for this app session');
  await user.click(screen.getByRole('checkbox',{name:'First'}));
  await user.click(screen.getByRole('checkbox',{name:'Second'}));
  const dates=screen.getAllByLabelText('Publish publicly at (local time)');
  fireEvent.change(dates[0],{target:{value:'2099-01-01T10:00'}});
  fireEvent.change(dates[1],{target:{value:'2099-01-02T11:00'}});
  const audiences=screen.getAllByLabelText('Made for kids?');
  await user.selectOptions(audiences[0],'no'); await user.selectOptions(audiences[1],'yes');
  await user.click(screen.getByRole('button',{name:'Upload and schedule selected'}));
  await screen.findByText(/2 video\(s\) uploaded/);
  expect(sent.map(x=>x.render_id)).toEqual(['a','b']);
  expect(sent.map(x=>x.title)).toEqual(['First','Second']);
  expect(sent.map(x=>x.publish_at)).toEqual([new Date('2099-01-01T10:00').toISOString(),new Date('2099-01-02T11:00').toISOString()]);
});
