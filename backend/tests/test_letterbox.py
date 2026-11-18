import pytest
from pydantic import ValidationError
from app.routes.rendering import RenderSettings
from app.services import captions, reframe, shorts


@pytest.mark.parametrize('width,height,expected',[(1920,1080,(1080,608)),(1440,1080,(1080,810)),(1000,1000,(1080,1080)),(1080,1920,(1080,1920))])
def test_letterbox_geometry(width,height,expected):
    rect=reframe.letterbox_rect(width,height)
    assert (rect['w'],rect['h'])==expected
    assert 0<=rect['x']<=1080-rect['w'] and 0<=rect['y']<=1920-rect['h']
    assert all(rect[key]%2==0 for key in ('x','y','w','h'))


def test_letterbox_bias_and_narrower_source_are_not_cropped():
    up=reframe.letterbox_rect(1920,1080,bias=-.15)
    down=reframe.letterbox_rect(1920,1080,bias=.15)
    assert down['y']>up['y']>=0 and down['y']+down['h']<=1920
    narrow=reframe.letterbox_rect(400,1000,bias=.15)
    assert narrow['h']==1920 and narrow['w']<1080 and narrow['y']==0
    with pytest.raises(ValueError): reframe.letterbox_rect(1920,1080,bias=.151)
    assert shorts.choose_layout('auto',.9,True,True)[0]=='stack'
    assert all(shorts.choose_layout('auto',v)[0]!='letterbox' for v in (.9,1.5,3))
    assert shorts.choose_layout('letterbox',2)[0]=='letterbox'


@pytest.mark.parametrize('color,valid',[('#1a2B3c',True),('red',False),('#12',False),('javascript:',False),('',False),('#abcdef\n',False)])
def test_letterbox_color_validation(color,valid):
    if valid: assert RenderSettings(letterbox_bg=color).letterbox_bg==color
    else:
        with pytest.raises(ValidationError): RenderSettings(letterbox_bg=color)


@pytest.mark.parametrize('style',['highlight','classic','minimal'])
def test_caption_on_video_position_and_fallback(style):
    rect=reframe.letterbox_rect(1280,720)
    layout=captions.on_video_layout(1080,1920,rect,style)
    assert layout['used']=='on_video'
    assert rect['y']<layout['y']<=rect['y']+rect['h']
    assert layout['y']-2.4*layout['size']>=rect['y']
    assert layout['y']<=1920*.8
    words=[{'start':0,'end':.5,'word':'hello'},{'start':.5,'end':1,'word':'world'}]
    ass=captions.build_ass(words,0,1,1080,1920,style,safe_zone=True,caption_layout=layout)
    assert f"\\pos({layout['x']},{layout['y']})" in ass and r'\an2' in ass
    smaller=captions.on_video_layout(1080,1920,{'x':0,'y':800,'w':1080,'h':95},style)
    assert smaller['used']=='on_video' and smaller['size']<layout['size'] and smaller['size']>=layout['size']*.7
    short=captions.on_video_layout(1080,1920,{'x':0,'y':800,'w':1080,'h':20},style)
    assert short['used']=='safe_zone'
