import pytest

from aio.util import human_percent


@pytest.mark.parametrize(('percent', 'text'), [
    (None, '—'),
    (0, '0%'),
    (100, '100%'),
    (99.998707, '99.9987%'),
    (99.9999999, '>99.9999%'),
    (0.0000001, '<0.0001%'),
    (42.5, '42.5%'),
])
def test_percentage_does_not_round_partial_quota_to_full_or_empty(percent, text):
    assert human_percent(percent) == text
