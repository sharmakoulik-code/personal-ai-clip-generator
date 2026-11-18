// @vitest-environment jsdom
import {render,screen,waitFor,cleanup} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import MetadataCard from './MetadataCard';
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.restoreAllMocks();});
it('requires opt-in for Cloud AI and copies generated metadata',async()=>{
  const fetcher=vi.fn(async (_url:RequestInfo|URL,init?:RequestInit)=>init?.method==='POST'
    ? new Response(JSON.stringify({titles:['A title'],description:'A summary',hashtags:['#one','#two','#three'],provider:'Groq'}),{status:200})
    : new Response('{}',{status:404}));
  vi.stubGlobal('fetch',fetcher);
  const user=userEvent.setup();const copy=vi.spyOn(navigator.clipboard,'writeText').mockResolvedValue();
  render(<MetadataCard clipId="clip1"/>);
  await user.click(screen.getByLabelText('Use Cloud AI for metadata'));
  await user.click(screen.getByRole('button',{name:'Generate metadata'}));
  await screen.findByText(/Provider: Groq/);
  await user.click(screen.getByRole('button',{name:'Copy title 1'}));
  await user.click(screen.getByRole('button',{name:'Copy description'}));
  await user.click(screen.getByRole('button',{name:'Copy hashtags'}));
  expect(copy).toHaveBeenCalledWith('A title');expect(copy).toHaveBeenCalledWith('A summary');expect(copy).toHaveBeenCalledWith('#one #two #three');
  await waitFor(()=>expect(fetcher).toHaveBeenCalledWith('/api/clips/clip1/metadata',expect.objectContaining({method:'POST',body:expect.stringContaining('"cloud":true')})));
});
