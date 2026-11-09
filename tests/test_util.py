import pytest

from aio.util import human_percent


@pytest.mark.parametrize(('percent', 'text'), [
    (None, '—'),
    (0, '0%'),
    (100, '100%'),
    (99.998707, '99%'),
    (70.2347, '70%'),
    (0.0000001, '<1%'),
    (42.6, '43%'),
])
def test_percentage_does_not_round_partial_quota_to_full_or_empty(percent, text):
    assert human_percent(percent) == text
