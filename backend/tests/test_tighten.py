import pytest
from app.services import tighten, captions


def word(a,b,text): return {'start':a,'end':b,'word':text}


def test_leading_filler_long_pause_and_remap():
    words=[word(0,.2,'Um,'),word(.25,.5,' hello'),word(.55,1,'science'),word(2,2.5,'works.'),word(3,3.5,'Indeed.')]
    intervals=tighten.plan_cuts(words,0,5)
    assert intervals[0][0]==pytest.approx(.51)
    assert len(intervals)>=2
    for a,b in intervals[:-1]:
        assert all(not (w['start']<b<w['end']) for w in words)
    assert sum(b-a for a,b in intervals)>=3.5-1e-8
    remapped=tighten.remap_words(words,intervals)
    assert remapped[0]['word']=='science' and remapped[0]['start']==pytest.approx(.04)
    assert 'science works.' in captions.build_srt(remapped,0,sum(b-a for a,b in intervals))
    assert tighten.remap_time(intervals[1][0],intervals)==pytest.approx(intervals[0][1]-intervals[0][0])


def test_pause_cap_overlaps_fast_speech_and_no_words():
    words=[word(0,.9,'one'),word(.3,1.5,'two'),word(1.6,2,'three')]
    assert tighten.plan_cuts(words,0,2)==[(0,2)]
    assert tighten.plan_cuts([],0,10)==[(0,10)]
    long=[word(0,1,'one'),word(8,9,'two')]
    assert tighten.plan_cuts(long,0,10)==[(0,10)]
    with pytest.raises(ValueError): tighten.plan_cuts(words,2,1)


def test_snap_ending_preserves_words_and_handles_missing_punctuation():
    assert tighten.snap_ending([word(5,6,'ends.'),word(6.1,6.5,'next')],8)==pytest.approx(6.5)
    assert tighten.snap_ending([word(1,2,'old.'),word(7,7.5,'more')],8)==8
    assert tighten.snap_ending([word(7,7.5,'end!')],8)==pytest.approx(7.65)
    assert tighten.snap_ending([word(7,7.95,'late?')],8)==8


def test_tail_energy_bonus_conditions():
    import numpy as np
    from app.services import clip_selection
    voice=np.full(16000,.03,dtype=np.float32); silent=np.zeros(16000,dtype=np.float32)
    assert clip_selection.tail_speech_energy(voice,8000,[word(8.5,9.5,'speech')],10)
    assert not clip_selection.tail_speech_energy(silent,8000,[word(8.5,9.5,'speech')],10)
    assert not clip_selection.tail_speech_energy(voice,8000,[word(4,5,'speech')],10)


def test_tail_energy_bonus_applied_only_when_audible_and_timed(tmp_path,monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from app.services import clip_selection
    monkeypatch.setenv('CLIP_STORAGE_PATH',str(tmp_path))
    source=tmp_path/'videos'/'1'/'v.mp4'; source.parent.mkdir(parents=True); source.write_bytes(b'x')
    samples=np.full(16000,.03,dtype='<f4').tobytes()
    monkeypatch.setattr(clip_selection.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=samples))
    clip={'start':0.,'end':10.,'score':50,'scores':{}}
    result=clip_selection.score_tail_audio(source,[clip],[word(8.5,9.5,'hello')])
    assert result[0]['score']==53 and result[0]['scores']['speech_energy_bonus']==3
    clip={'start':0.,'end':10.,'score':50,'scores':{}}
    assert clip_selection.score_tail_audio(source,[clip],[word(2,3,'hello')])[0]['score']==50
