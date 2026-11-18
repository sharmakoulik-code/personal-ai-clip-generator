import cv2
import numpy as np
import pytest
from app.services import preflight


def info(**overrides):
    return {'width':1920,'height':1080,'duration':120,'frame_rate':'30/1','nominal_frame_rate':'30/1','has_audio':True} | overrides


def codes(result): return {w['code']:w['severity'] for w in result['warnings']}


def test_metadata_warnings_and_rate_validation():
    result=preflight.inspect(info(width=540,height=960,duration=20,has_audio=False,frame_rate='30/1',nominal_frame_rate='25/1'),[],[])
    found=codes(result)
    assert found['already_vertical']=='info' and found['low_resolution']=='warn'
    assert found['too_short']=='info' and found['no_audio']=='block'
    assert found['no_faces_detected']=='info' and found['variable_frame_rate']=='info'
    assert not result['ok']
    assert codes(preflight.inspect(info(frame_rate='7/1'),[],[1]))['odd_frame_rate']=='info'
    assert preflight.frame_rate('1/0') is None and preflight.frame_rate('nan') is None
    with pytest.raises(ValueError): preflight.inspect(info(width=0),[],[])


def test_caption_band_and_plain_changing_video():
    frames=[]; plain=[]
    for i in range(12):
        image=np.full((180,320,3),30+i*12,dtype=np.uint8)
        plain.append(image.copy())
        cv2.putText(image,'CAPTION TEXT',(55,150),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),2)
        frames.append(image)
    assert preflight.caption_band(frames)
    assert not preflight.caption_band(plain)
    assert 'possible_burned_in_captions' in codes(preflight.inspect(info(),frames,[1]*12))
    static=[frames[0]]*12
    assert not preflight.caption_band(static)


def test_high_cut_rate_and_stable_source():
    frames=[np.full((180,320,3),(0,0,255) if i%2 else (255,0,0),dtype=np.uint8) for i in range(12)]
    assert 'high_cut_rate' in codes(preflight.inspect(info(duration=20),frames,[1]*12))
    assert 'high_cut_rate' not in codes(preflight.inspect(info(duration=120),[frames[0]]*12,[1]*12))


def test_low_resolution_upscale_and_normal_metadata():
    assert 'low_resolution' in codes(preflight.inspect(info(width=1280,height=720),[],[1]))
    result=preflight.inspect(info(width=3840,height=2160),[],[1])
    assert 'low_resolution' not in codes(result) and result['ok']
    assert result['stats']['estimated_follow_upscale'] < 1
