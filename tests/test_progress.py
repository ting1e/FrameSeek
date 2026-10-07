from frameseek.progress import ProcessingProgress


def test_eta_requires_stable_window_and_uses_saved_frames():
    tracker = ProcessingProgress()
    for seconds, completed in [(0,100),(10,120),(20,140)]:
        result = tracker.update(completed, 60, 'scope', now=seconds)
        assert result['eta_seconds'] is None
    result = tracker.update(160,60,'scope',now=30)
    assert result['frames_per_second'] == 2
    assert result['eta_seconds'] == 30
    assert result['completed_frames'] == 160
    assert result['stable'] is True


def test_eta_suppressed_on_pause_failure_scope_change_and_reset():
    tracker = ProcessingProgress()
    for seconds in (0,10,20,30): result = tracker.update(seconds*2,100,'one',now=seconds)
    assert result['eta_seconds'] == 50
    blocked = tracker.update(80,100,'one',blocked=True,now=40)
    assert blocked['eta_seconds'] is None and blocked['state'] == 'blocked'
    paused = tracker.update(80,100,'one',paused=True,now=50)
    assert paused['frames_per_second'] is None and paused['state'] == 'paused'
    resumed = tracker.update(80,100,'one',now=60)
    assert resumed['eta_seconds'] is None
    assert tracker.update(100,100,'new',now=70)['eta_seconds'] is None
    assert tracker.update(10,100,'new',now=80)['frames_per_second'] is None
    idle = tracker.update(10,0,'new',active=False,now=90)
    assert idle['state'] == 'idle' and idle['eta_seconds'] is None


def test_uneven_speed_and_duplicate_polls_do_not_make_eta():
    tracker = ProcessingProgress()
    for seconds, completed in [(0,0),(10,10),(20,200),(30,200)]:
        result = tracker.update(completed, 200, 'one', now=seconds)
    assert result['eta_seconds'] is None
    assert result['stable'] is False
    assert tracker.update(200,200,'one',now=31)['stable'] is False


def test_chunked_saves_can_stabilize_without_continuous_cursor_changes():
    tracker = ProcessingProgress()
    for seconds in range(0,61,10):
        result = tracker.update((seconds//30)*64,128,'one',now=seconds)
    assert result['stable'] is True
    assert result['eta_seconds'] == 60
    result = tracker.update(128,128,'one',now=100)
    assert result['eta_seconds'] is None
