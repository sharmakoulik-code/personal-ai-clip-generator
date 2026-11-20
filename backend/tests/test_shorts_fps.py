"""Pure tests for Shorts output frame-rate selection."""
import pytest
from app.services.shorts import output_fps


@pytest.mark.parametrize('rate,expected', [
    ('30000/1001', 30), ('60000/1001', 60), ('24000/1001', 24),
    ('25', 25), ('30', 30), ('48', 30), ('50', 50), ('60', 60),
    ('15', 15), ('20', 20), ('120', 60),
    ('597/20', 30), ('29849/1000', 25),  # exact 0.5% boundary and just below
    ('597/25', 24), ('23879/1000', 23.879),
    ('995/20', 50), ('49749/1000', 30),
    ('597/10', 60), ('59699/1000', 50),
    ('20/3', 20/3), ('1000', 60),
])
def test_output_fps_selection(rate, expected):
    assert output_fps(rate) == pytest.approx(expected)


@pytest.mark.parametrize('rate', [None, '', 'bad', '0/0', '1/0', '0', '-1', 'nan', 'inf', '-inf', '1001'])
def test_output_fps_invalid(rate):
    with pytest.raises(ValueError):
        output_fps(rate)
