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
