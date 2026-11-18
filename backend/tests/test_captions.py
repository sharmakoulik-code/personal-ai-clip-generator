import pytest
from app.services import captions


def words():
    return [{'start':10,'end':10.5,'word':' Hello'}, {'start':10.4,'end':10.8,'word':' world.'}, {'start':11.4,'end':11.8,'word':' Again'}, {'start':12,'end':12,'word':'ignored'}]


def test_timing_overlap_and_srt_format():
    srt=captions.build_srt(words(),10,12)
    assert '00:00:00,000 --> 00:00:00,800' in srt
    assert '00:00:01,400 --> 00:00:01,800' in srt
    ass=captions.build_ass(words(),10,12,1080,1920)
    assert '0:00:00.00,0:00:00.40' in ass
    assert '0:00:00.40,0:00:00.80' in ass
    assert 'ignored' not in ass


def test_caption_styles_escape_uppercase_and_grouping():
    data=[{'start':i*.3,'end':i*.3+.2,'word':s} for i,s in enumerate(['hi',r'\\bad{\\b1}', 'this','is','a','long','sentence','now'])]
    hi=captions.build_ass(data,0,5,1080,1920,'highlight',True)
    classic=captions.build_ass(data,0,5,1080,1920,'classic')
    minimal=captions.build_ass(data,0,5,720,1280,'minimal')
    assert 'HI' in hi and r'\{' in hi and r'\}' in hi and r'\\\\BAD' in hi
    assert hi.count('Dialogue:')==8 and '{\\c&H00FFFF&' in hi
    assert '{\\c&H00FFFF&' not in classic and classic.count('Dialogue:') < 8
    assert 'PlayResY: 1280' in minimal and '\\N' in classic


def test_caption_empty_fails_and_times_are_clamped():
    with pytest.raises(ValueError,match='No timed'): captions.build_ass(words(),20,30,720,1280)
    entries=captions._clean([{'start':-3,'end':20,'word':'long'}],0,5)
    assert entries[0]['start']==0 and entries[0]['end']==2.5


@pytest.mark.parametrize('style', ['highlight','classic','minimal'])
def test_shorts_caption_safe_area_and_short_groups(style):
    entries=[{'start':i*.3,'end':i*.3+.25,'word':s} for i,s in enumerate(['one','two','three','four','five','六七八九十','supercalifragilisticexpialidocious'])]
    ass=captions.build_ass(entries,0,4,1080,1920,style,safe_zone=True)
    assert r'{\an5\pos(497,1190)}' in ass
    style_line=next(line for line in ass.splitlines() if line.startswith('Style: Default'))
    fields=style_line.split(',')
    assert fields[18]=='5' and int(fields[19])>=65 and int(fields[20])>=151
    groups=captions._short_groups(captions._clean(entries,0,4))
    assert all(len(g)<=3 for g in groups)
    for group in groups:
        lines=['']
        for item in group:
            if item['line_break']: lines.append('')
            lines[-1]+= (' ' if lines[-1] else '')+item['text']
        assert len(lines)<=2 and all(len(line)<=18 for line in lines)


def test_vtt_header_and_dot_timing_preserves_commas():
    vtt=captions.build_vtt([{'start':0.1,'end':.5,'word':'hello, friend'}],0,1)
    assert vtt.startswith('WEBVTT\n\n') and '00:00:00.100 --> 00:00:00.500' in vtt and 'hello, friend' in vtt
