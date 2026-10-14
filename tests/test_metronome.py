"""Tests for Metronome._callback.

All tests exercise the pure callback logic only — no audio hardware needed.
The conftest.py at the project root stubs out sounddevice before import.
"""

import numpy as np

from taperoll.config import MetronomeConfig
from taperoll.metronome import Metronome

SAMPLE_RATE = 48000
CLICK_DUR = MetronomeConfig().click_dur
CLICK_LEN = int(SAMPLE_RATE * CLICK_DUR)  # e.g. 720 samples at 48 kHz / 15 ms


# ─── helpers ──────────────────────────────────────────────────────────────────


def invoke(m: Metronome, frames: int, channels: int = 1) -> np.ndarray:
    """Run one metronome callback and return the output buffer."""
    outdata = np.zeros((frames, channels), dtype=np.float32)
    m._callback(outdata, frames, None, None)
    return outdata


def period_for(bpm: int) -> int:
    return max(1, int(SAMPLE_RATE * 60.0 / bpm))


# ─── basic firing ─────────────────────────────────────────────────────────────


def test_click_fires_at_sample_zero_when_countdown_is_zero():
    """When _countdown == 0 the click must start at position 0."""
    m = Metronome()
    m._countdown = 0
    buf = invoke(m, 4800)
    # Sample 0 is sin(0)=0; sample 1 is the first audible sample.
    assert buf[1, 0] > 0.05, "Expected click energy at sample 1"
    # Silence must follow after the click body.
    assert np.all(buf[CLICK_LEN:, 0] == 0.0), "Expected silence after click"


def test_click_fires_at_correct_offset():
    """Click must start exactly at the sample indicated by _countdown."""
    m = Metronome()
    countdown = 200
    m._countdown = countdown
    buf = invoke(m, 4800)
    # Silence before the click.
    assert np.all(buf[:countdown, 0] == 0.0), "Expected silence before click"
    # Click body present immediately after countdown.
    click_end = countdown + CLICK_LEN
    assert np.max(np.abs(buf[countdown:click_end, 0])) > 0.05


def test_countdown_updated_correctly_after_callback():
    """After one callback, _countdown must reflect samples remaining to next beat."""
    m = Metronome()
    m.bpm = 120
    m._countdown = 0
    buf_size = 512
    invoke(m, buf_size)
    expected_countdown = period_for(120) - buf_size
    assert m._countdown == expected_countdown


# ─── click-tail carry-over (dropout regression) ───────────────────────────────


def test_tail_stored_when_click_extends_past_buffer():
    """When a click starts near the end of a buffer its tail must be saved."""
    m = Metronome()
    buf_size = 50  # smaller than CLICK_LEN (720)
    offset = 45  # click starts at sample 45 → only 5 samples fit
    m._countdown = offset

    invoke(m, buf_size)

    assert m._pending_click is not None, "_pending_click should be set"
    expected_tail_len = CLICK_LEN - (buf_size - offset)  # 720 - 5 = 715
    assert len(m._pending_click) == expected_tail_len


def test_partial_click_written_correctly_before_boundary():
    """The samples that DO fit in the first buffer must match the click waveform."""
    m = Metronome()
    m.beats_per_bar = 1  # use normal click (no accent) for predictable comparison
    buf_size = 50
    offset = 45  # 5 samples fit
    m._countdown = offset

    buf = invoke(m, buf_size)

    # Samples before the click should be silent.
    assert np.all(buf[:offset, 0] == 0.0)
    # Samples from offset to end of buffer should match the first 5 samples of the normal click.
    np.testing.assert_array_almost_equal(
        buf[offset:, 0], m._click_normal[: buf_size - offset]
    )


def test_tail_plays_at_start_of_next_buffer():
    """The carried-over tail must be written to the beginning of the next buffer."""
    m = Metronome()
    buf_size = 50
    offset = 45
    m._countdown = offset

    invoke(m, buf_size)

    # Snapshot the tail before the second call consumes it.
    assert m._pending_click is not None
    tail_snapshot = m._pending_click.copy()

    # The next click is far away (period >> buf_size), so only the tail plays.
    buf2 = invoke(m, buf_size)

    samples_applied = min(len(tail_snapshot), buf_size)
    np.testing.assert_array_almost_equal(
        buf2[:samples_applied, 0], tail_snapshot[:samples_applied]
    )


def test_tail_fully_consumed_after_enough_buffers():
    """After enough buffers, _pending_click must be cleared."""
    m = Metronome()
    buf_size = 50
    m._countdown = 49  # 1 sample fits → 719-sample tail

    invoke(m, buf_size)
    assert m._pending_click is not None

    # Run until the tail is fully consumed (ceil(719/50) = 15 more buffers).
    for _ in range(20):
        invoke(m, buf_size)

    assert m._pending_click is None, (
        "_pending_click should be cleared once tail is played"
    )


# ─── stereo output ────────────────────────────────────────────────────────────


def test_click_written_to_both_channels_in_stereo():
    m = Metronome()
    m._countdown = 0
    buf = invoke(m, 4800, channels=2)
    np.testing.assert_array_equal(buf[:, 0], buf[:, 1])


# ─── beat index and time signatures ───────────────────────────────────────────


def test_beat_index_increments_each_click():
    """Each click must advance _beat_idx by 1."""
    m = Metronome()
    m.beats_per_bar = 4
    m._countdown = 0
    # Use buf_size < period so only the first click (at sample 0) fires.
    buf_size = period_for(m.bpm) - 1

    assert m._beat_idx == 0
    invoke(m, buf_size)
    assert m._beat_idx == 1


def test_beat_index_wraps_at_bar_boundary():
    """_beat_idx must wrap back to 0 after beats_per_bar clicks."""
    m = Metronome()
    m.beats_per_bar = 3
    m._countdown = 0
    # buf_size < period ensures exactly one click fires per invoke call.
    one_beat = period_for(m.bpm) - 1
    for expected_next in [1, 2, 0, 1]:
        invoke(m, one_beat)
        assert m._beat_idx == expected_next


def test_no_accent_in_one_one_time_signature():
    """1/1 must always use the normal click, never the accent click."""
    m = Metronome()
    m.beats_per_bar = 1
    m._countdown = 0
    buf = invoke(m, 4800)
    # The accent click has higher frequency so different sample values.
    # Check that the written samples match _click_normal, not _click_accent.
    np.testing.assert_array_almost_equal(buf[:CLICK_LEN, 0], m._click_normal)


# ─── timing accuracy ──────────────────────────────────────────────────────────


def test_beat_timing_is_sample_accurate_over_many_buffers():
    """Beats must fire at integer multiples of the period, never drifting.

    Instead of scanning waveform data (which is contaminated by click tails
    spanning buffer boundaries), we read _countdown *before* each callback.
    When a beat fires inside a buffer, it fires at sample `prev_countdown`
    within that buffer, i.e. at global position `total + prev_countdown`.
    """
    m = Metronome()
    m.bpm = 120
    buf_size = 512
    p = period_for(120)  # 24000 samples

    beat_positions = []
    total = 0
    for _ in range(300):  # covers ~6 beats at 120 BPM with 512-sample buffers
        prev_countdown = m._countdown
        prev_beat_idx = m._beat_idx
        invoke(m, buf_size)
        if m._beat_idx != prev_beat_idx:
            beat_positions.append(total + prev_countdown)
        total += buf_size

    assert len(beat_positions) >= 2, "Expected at least 2 beats in the window"
    for i, pos in enumerate(beat_positions):
        expected = i * p
        assert abs(pos - expected) <= 1, (
            f"Beat {i} fired at sample {pos}, expected ~{expected} (drift > 1 sample)"
        )


# ─── subdivisions ──────────────────────────────────────────────────────────────


def test_subdivision_off_by_default():
    m = Metronome()
    assert m.subdivision is False


def test_subdivision_click_is_quieter_than_normal_click():
    m = Metronome()
    assert np.max(np.abs(m._click_sub)) < np.max(np.abs(m._click_normal))


def test_subdivision_off_fires_no_extra_click_at_half_period():
    """Regression: with subdivision off, only real beats fire (no click at the midpoint)."""
    m = Metronome()
    m.bpm = 120
    m.subdivision = False
    m._countdown = 0
    m._beat_idx = 0
    p = period_for(120)
    buf = invoke(m, p)
    half = p // 2
    assert np.all(buf[half : half + 5, 0] == 0.0)


def test_subdivision_on_fires_click_at_half_period():
    """With subdivision on, a quiet click fires halfway between beats."""
    m = Metronome()
    m.bpm = 120
    m.beats_per_bar = 4
    m.subdivision = True
    m._countdown = 0
    m._beat_idx = 0
    p = period_for(120)
    half = p // 2

    buf = invoke(m, p + 100)

    assert np.max(np.abs(buf[0:CLICK_LEN, 0])) > 0.01, "Expected click at sample 0"
    assert np.max(np.abs(buf[half : half + CLICK_LEN, 0])) > 0.01, (
        "Expected subdivision click at the half-period mark"
    )


def test_subdivision_sub_click_matches_click_sub_waveform():
    """The click fired between beats must be the quiet sub-click, not the normal one."""
    m = Metronome()
    m.subdivision = True
    m._countdown = 10_000_000  # far away — only the sub-tick fires in this window
    m._sub_countdown = 500

    buf = invoke(m, 500 + CLICK_LEN + 10)

    np.testing.assert_array_almost_equal(buf[500 : 500 + CLICK_LEN, 0], m._click_sub)


def test_beat_idx_only_counts_real_beats_when_subdivision_on():
    """_beat_idx must wrap on beats_per_bar alone — sub-ticks must never advance it.

    Same buffer sizing as test_beat_index_wraps_at_bar_boundary (one_beat = period-1),
    so the expected sequence is identical whether or not subdivision is enabled.
    """
    m = Metronome()
    m.beats_per_bar = 3
    m.subdivision = True
    m._countdown = 0
    m._sub_countdown = period_for(m.bpm) // 2
    one_beat = period_for(m.bpm) - 1
    for expected_next in [1, 2, 0, 1]:
        invoke(m, one_beat)
        assert m._beat_idx == expected_next


def test_subdivision_realigns_to_new_tempo_after_bpm_change():
    """Regression: after a tempo change, the subdivision must re-lock to exactly
    half of the *new* period the next time a beat fires — not stay offset by half
    of the *old* period, which produced audible 'swing' instead of even eighth notes.
    """
    m = Metronome()
    m.beats_per_bar = 4
    m.subdivision = True
    m.bpm = 120
    m._countdown = 0
    m._beat_idx = 0
    m._sub_countdown = period_for(120) // 2

    # Fire beat 0 and its subdivision at the original tempo.
    invoke(m, period_for(120))

    # Slow down before the next beat fires.
    m.bpm = 60
    p_new = period_for(m.bpm)

    # Land exactly on the sample where the already-pending next beat fires.
    pending = m._countdown
    invoke(m, pending + 1)

    # Right after that beat fires, the subdivision must be anchored to half of the
    # *new* period — not a stale offset carried over from the old tempo.
    assert abs(m._sub_countdown - p_new // 2) <= 1


def test_toggling_subdivision_does_not_disturb_beat_schedule():
    """Flipping `subdivision` mid-stream must not change _countdown/_beat_idx at all —
    the whole point is a seamless toggle with no restart."""
    m = Metronome()
    m._countdown = 12345
    m._beat_idx = 2
    m._sub_countdown = 6789

    m.subdivision = True

    assert m._countdown == 12345
    assert m._beat_idx == 2
    assert m._sub_countdown == 6789
