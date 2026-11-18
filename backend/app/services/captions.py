"""Safe, local subtitles from source-time word timestamps."""
import math
import os
import re


def _clean(words, clip_start, clip_end, uppercase=False):
    entries=[]
    for word in words:
        try: start,end=float(word['start']),float(word['end']); text=str(word['word']).strip()
        except (KeyError, TypeError, ValueError): continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start or end <= clip_start or start >= clip_end: continue
        text=' '.join(text.split())
        text=''.join(c for c in text if ord(c)>=32)[:200]
        if text: entries.append({'start':max(start,clip_start)-clip_start,'end':min(end,clip_end,max(start,clip_start)+2.5)-clip_start,'text':text.upper() if uppercase else text})
    entries.sort(key=lambda w:(w['start'],w['end']))
    for i in range(len(entries)-1): entries[i]['end']=min(entries[i]['end'],entries[i+1]['start'])
    entries=[w for w in entries if w['end']>w['start']]
    if not entries: raise ValueError('No timed transcript words in this clip range; cannot create captions.')
    return entries


def _groups(entries):
    groups=[]; group=[]; line_words=0; line_chars=0; lines=1
    for word in entries:
        text=word['text']; start_new_line=group and (line_words>=4 or line_chars+1+len(text)>20)
        split = group and (word['start']-group[-1]['end']>.4 or word['end']-group[0]['start']>2.5 or re.search(r'[.!?。！？]["\'”’)]*$',group[-1]['text']) or (start_new_line and lines>=2))
        if split: groups.append(group); group=[]; line_words=0; line_chars=0; lines=1; start_new_line=False
        if start_new_line: lines+=1; line_words=0; line_chars=0
        item=dict(word,line_break=bool(start_new_line)); group.append(item)
        line_words+=1; line_chars+=len(text)+(1 if line_words>1 else 0)
    if group: groups.append(group)
    return groups


def _ass_time(seconds):
    centis=round(max(0.,seconds)*100); h,rest=divmod(centis,360000); m,rest=divmod(rest,6000); s,cs=divmod(rest,100)
    return f'{h}:{m:02d}:{s:02d}.{cs:02d}'


def _srt_time(seconds):
    millis=round(max(0.,seconds)*1000); h,rest=divmod(millis,3600000); m,rest=divmod(rest,60000); s,ms=divmod(rest,1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def _escape(text):
    # Do not allow transcript text to form ASS override tags or control escapes.
    return text.replace('\\','\\\\').replace('{','\\{').replace('}','\\}')


def _line(group, active=None, fontsize=72):
    parts=[]
    for i,w in enumerate(group):
        if i: parts.append('\\N' if w['line_break'] else ' ')
        text=_escape(w['text'])
        if i==active: text='{\\c&H00FFFF&\\fs'+str(round(fontsize*1.08))+'}'+text+'{\\r}'
        parts.append(text)
    return ''.join(parts)


def build_ass(words, clip_start, clip_end, width, height, style='highlight', uppercase=False):
    if style not in ('highlight','classic','minimal'): raise ValueError('Unknown caption style.')
    groups=_groups(_clean(words,clip_start,clip_end,uppercase))
    font=re.sub(r'[,{}\\\r\n]','',os.environ.get('CLIP_CAPTION_FONT','Arial')).strip()[:100] or 'Arial'
    size=round(height*(.0375 if style!='minimal' else .029)); outline=max(1,round(height*.0024)); margin=round(height*(.20 if style!='minimal' else .14)); side=round(width*.08)
    header=f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},2,2,{side},{side},{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events=[]
    for group in groups:
        if style=='highlight':
            for i,w in enumerate(group): events.append(f"Dialogue: 0,{_ass_time(w['start'])},{_ass_time(w['end'])},Default,,0,0,0,,{_line(group,i,size)}")
        else: events.append(f"Dialogue: 0,{_ass_time(group[0]['start'])},{_ass_time(group[-1]['end'])},Default,,0,0,0,,{_line(group)}")
    return header+'\n'.join(events)+'\n'


def build_srt(words, clip_start, clip_end):
    groups=_groups(_clean(words,clip_start,clip_end))
    lines=[]
    for index,group in enumerate(groups,1):
        text=''.join(('\n' if w['line_break'] else ' ') + w['text'] if i else w['text'] for i,w in enumerate(group))
        lines.append(f"{index}\n{_srt_time(group[0]['start'])} --> {_srt_time(group[-1]['end'])}\n{text}\n")
    return '\n'.join(lines)
