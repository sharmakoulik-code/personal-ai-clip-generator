"""Pure, conservative source-time cut planning for timed words."""
import math
import re

_FILLERS={'um','uh','er','ah','hmm','so','okay','alright','like','welcome','hey','hi','hello'}


def _words(words, start, end):
    result=[]
    for word in words:
        try:
            a=float(word['start']); b=float(word['end']); text=word['word']
            if not isinstance(text,str) or not math.isfinite(a) or not math.isfinite(b) or b<=a or b<=start or a>=end: continue
            result.append((max(start,a),min(end,b),text,word))
        except (KeyError,TypeError,ValueError,OverflowError): continue
    return sorted(result,key=lambda x:(x[0],x[1]))


def _filler(text):
    return re.sub(r'[^\w\s]', '',text.casefold()).strip() in _FILLERS


def plan_cuts(words, clip_start, clip_end, *, trim_leading_fillers=True, cut_pauses=True, max_pause=.6, keep_pause=.25, min_pause_to_cut=.5):
    if not all(math.isfinite(x) for x in (clip_start,clip_end,max_pause,keep_pause,min_pause_to_cut)) or clip_end<=clip_start or min(max_pause,keep_pause,min_pause_to_cut)<0:
        raise ValueError('Invalid cut-planning range or pause settings.')
    clean=_words(words,clip_start,clip_end)
    if not clean: return [(clip_start,clip_end)]
    max_removed=.3*(clip_end-clip_start)
    first=0
    if trim_leading_fillers:
        while first<len(clean):
            if _filler(clean[first][2]): first+=1; continue
            if first+1<len(clean) and re.sub(r'[^\w]','',clean[first][2].casefold())=='you' and re.sub(r'[^\w]','',clean[first+1][2].casefold())=='know': first+=2; continue
            break
    # Never remove all speech, overlapping words, or more than the total budget.
    begin=clip_start
    if 0<first<len(clean):
        proposed=max(clip_start,clean[first][0]-.04)
        if proposed-clip_start<=max_removed and all(w[1]<=proposed for w in clean[:first]): begin=proposed
        else: first=0
    removals=[]
    if cut_pauses:
        # A running maximum handles overlapping word times without cutting either word.
        prev_end=clean[first][1]
        for a,b,_,_ in clean[first+1:]:
            gap=a-prev_end
            retained=max(.08,min(keep_pause,max_pause))
            remove=gap-retained
            if gap>min_pause_to_cut and remove>0:
                left=prev_end+(gap-remove)/2; right=a-(gap-remove)/2
                if left>=prev_end+.04-1e-9 and right<=a-.04+1e-9 and right>left:
                    removals.append((left,right,gap))
            prev_end=max(prev_end,b)
    # Raising the threshold discards the shortest pauses first.
    removals.sort(key=lambda cut: cut[2])
    budget=max_removed-(begin-clip_start)
    while removals and sum(right-left for left,right,_ in removals)>budget+1e-9:
        # Equal length gaps share a threshold, so discard them together.
        threshold=removals[0][2]
        removals=[cut for cut in removals if cut[2]>threshold]
    chosen=[(left,right) for left,right,_ in removals]
    result=[]; pos=begin
    for a,b in sorted(chosen):
        if a>pos: result.append((pos,a))
        pos=b
    if clip_end>pos: result.append((pos,clip_end))
    return result or [(clip_start,clip_end)]


def remap_time(t, keep_intervals):
    """Map dropped times to the adjacent join; clamp outside the clip."""
    elapsed=0.
    for a,b in keep_intervals:
        if t<a: return elapsed
        if t<=b: return elapsed+t-a
        elapsed+=b-a
    return elapsed


def remap_words(words, keep_intervals):
    if not keep_intervals: return []
    result=[]
    for a,b,_,original in _words(words,keep_intervals[0][0],keep_intervals[-1][1]):
        for start,end in keep_intervals:
            left=max(a,start); right=min(b,end)
            if right>left:
                result.append(dict(original,start=remap_time(left,keep_intervals),end=remap_time(right,keep_intervals)))
    return result


def snap_ending(words, clip_end):
    """Snap to the last nearby sentence ending, preserving a small tail.

    If a later word straddles the proposed boundary, extend through that word
    rather than cutting it in half; never extend beyond the original clip end.
    """
    if not math.isfinite(clip_end): raise ValueError('Invalid clip end.')
    valid=_words(words,max(0,clip_end-3),clip_end)
    punct=[b for a,b,text,_ in valid if re.search(r'[.!?。！？]["\'”’)]*$',text.strip())]
    if not punct: return clip_end
    target=min(clip_end,max(punct)+.15)
    for a,b,_,_ in valid:
        if a < target < b: target=min(clip_end,b)
    return target
