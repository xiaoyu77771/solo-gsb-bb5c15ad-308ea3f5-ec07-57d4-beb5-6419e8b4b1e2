from app.segments import fit_segments, map_x


def test_two_piece_with_jump():
    # 前段 y = 1.02x + 5，后段 y = 0.98x + 12
    anchors = []
    for x in range(0, 60, 10):
        anchors.append((float(x), 1.02 * x + 5.0))
    for x in range(200, 260, 10):
        anchors.append((float(x), 0.98 * x + 12.0))

    segments = fit_segments(anchors, residual_tol=0.01, slope_clip=2.0)
    assert len(segments) == 2
    assert abs(segments[0].slope - 1.02) < 1e-9
    assert abs(segments[0].intercept - 5.0) < 1e-7
    assert abs(segments[1].slope - 0.98) < 1e-9
    assert abs(segments[1].intercept - 12.0) < 1e-7


def test_single_segment_constant_offset():
    anchors = [(float(x), x + 30.0) for x in range(0, 100, 10)]
    segments = fit_segments(anchors, residual_tol=0.01)
    assert len(segments) == 1
    assert abs(segments[0].slope - 1.0) < 1e-9
    assert abs(segments[0].intercept - 30.0) < 1e-8


def test_map_extrapolation_and_gap():
    # 第一段 y=x；第二段 y=2x，两段之间有 90 秒宽空隙
    anchors = [(0.0, 0.0), (10.0, 10.0), (100.0, 200.0), (110.0, 220.0), (120.0, 240.0)]
    segments = fit_segments(anchors, residual_tol=0.01)
    assert len(segments) == 2
    assert abs(segments[1].slope - 2.0) < 1e-9
    # 段内
    y, ka, kb, _ = map_x(segments, 5.0)
    assert ka == kb == 0
    assert abs(y - 5.0) < 1e-9
    # 前段外推
    y, ka, _, _ = map_x(segments, -10.0)
    assert ka == 0
    # 段间空隙中点：逆距离混合（denom=gap=90），左段权重 0.5
    y, ka, kb, wa = map_x(segments, 55.0)
    assert (ka, kb) == (0, 1)
    assert abs(wa - 0.5) < 1e-9
    assert abs(y - (0.5 * 55.0 + 0.5 * (2.0 * 55.0))) < 1e-9
    # 空隙深处靠近左边界：左段占绝对主导
    _, _, _, wa_near_left = map_x(segments, 15.0)
    assert wa_near_left > 0.94
    # 后段外推
    y, ka, _, _ = map_x(segments, 500.0)
    assert ka == 1
    assert abs(y - 1000.0) < 1e-7


def test_no_segments_returns_identity():
    y, ka, kb, _ = map_x([], 42.0)
    assert y == 42.0
    assert ka is None and kb is None
