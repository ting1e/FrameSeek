from frameseek.evaluate import matches


def test_label_match_requires_source_path_and_time():
    label = {'source':'sda','relpath':'video.bif','time_ms':10000,'tolerance_ms':1000}
    assert matches({'source':'sda','relpath':'video.bif','time_ms':11000},label)
    assert not matches({'source':'sdc','relpath':'video.bif','time_ms':10000},label)
    assert not matches({'source':'sda','relpath':'other.bif','time_ms':10000},label)
    assert not matches({'source':'sda','relpath':'video.bif','time_ms':11001},label)
