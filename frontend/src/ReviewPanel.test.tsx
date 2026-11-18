// @vitest-environment jsdom
import {render, screen, waitFor, fireEvent, cleanup} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach, expect, it, vi} from 'vitest';
import ReviewPanel from './ReviewPanel';

afterEach(()=>{cleanup();vi.unstubAllGlobals();});

it('draws preview and safe zone, clamps trim and reverts',async()=>{
  const initial={id:'c1',start:1,end:6,title:'Example',selected:true};
  const fetcher=vi.fn(async (path:RequestInfo|URL,init?:RequestInit)=>{
    const url=String(path);
    const data=url==='/api/shorts/constants'?{safe_zone:{top:.08,bottom:.2,left:.06,right:.14}}:url.includes('preview-path')?{width:1000,height:500,layout:'follow',source_duration:10,path:[{t:1,x:100,y:0,w:280,h:500}]}:url.endsWith('/revert')?initial:{...initial,start:3};
    return {ok:true,json:async()=>data};
  });
  vi.stubGlobal('fetch',fetcher);
  const user=userEvent.setup(); const onUpdate=vi.fn(); const onOffset=vi.fn();
  render(<ReviewPanel clip={initial} videoId="v1" query="layout=follow" xOffset={0} setXOffset={onOffset} onUpdate={onUpdate}/>);
  const overlay=await screen.findByLabelText('Crop overlay');
  expect(overlay.style.left).toBe('10%');
  await user.click(screen.getByLabelText('Show safe zone'));
  expect(await screen.findByLabelText('Caption safe zone')).toBeTruthy();
  await user.click(screen.getByRole('button',{name:'Start +0.5 s'}));
  await waitFor(()=>expect(fetcher).toHaveBeenCalledWith('/api/clips/c1',expect.objectContaining({method:'PATCH',body:'{"start":1.5}'})));
  fireEvent.change(screen.getByLabelText('Crop x offset'),{target:{value:'.2'}});
  expect(onOffset).toHaveBeenCalledWith(.2);
  await user.click(screen.getByRole('button',{name:'Revert to AI pick'}));
  expect(fetcher).toHaveBeenCalledWith('/api/clips/c1/revert',{method:'POST'});
});

it('shows the letterbox canvas, video rectangle and canvas safe zone',async()=>{
  const initial={id:'bars',start:1,end:6,title:'Bars',selected:true};
  vi.stubGlobal('fetch',vi.fn(async(path:RequestInfo|URL)=>({ok:true,json:async()=>String(path).includes('constants')?{safe_zone:{top:.08,bottom:.2,left:.06,right:.14}}:{width:1280,height:720,layout:'letterbox',source_duration:10,canvas_width:1080,canvas_height:1920,video_rect:{x:0,y:694,w:1080,h:608},letterbox_bg:'#101820',path:[{t:1,x:0,y:0,w:1280,h:720}]}})));
  const user=userEvent.setup();
  render(<ReviewPanel clip={initial} videoId="v1" query="layout=letterbox" xOffset={0} setXOffset={()=>{}} onUpdate={()=>{}}/>);
  const overlay=await screen.findByLabelText('Crop overlay');
  expect(overlay.style.top).toBe(`${100*694/1920}%`);
  expect(overlay.style.height).toBe(`${100*608/1920}%`);
  expect(overlay.parentElement?.style.backgroundColor).toBe('rgb(16, 24, 32)');
  await user.click(screen.getByLabelText('Show safe zone'));
  expect(screen.getByLabelText('Caption safe zone').parentElement).toBe(overlay.parentElement);
});
