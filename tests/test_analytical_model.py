import math
from dataclasses import replace

import numpy as np
import pytest
import trimesh
from shapely import Polygon, box
from test_engine import parameters
from test_geometry import write_box

from bibazu_autotune.analytical_model import (
    AnalyticalModel,
    AnalyticalPressureModel,
    AutomaticOffsetEstimator,
    ModelConfiguration,
    NozzleMount,
    TippingMechanics,
    Turn,
    coverage_weights,
    drag_force,
    jet_hit_weights,
    jet_overlap_fractions,
    orient_body,
    resolve_turn,
    tipping_mechanics,
)
from bibazu_autotune.autotune import AutotunePipeline
from bibazu_autotune.geometry import analyze_stl
from bibazu_autotune.trial_context import manual_context


def configured(**values):
    config = ModelConfiguration(**values)
    return replace(
        config,
        mounts=tuple(
            replace(m, row_center_m=0.015 if m.normal_axis == "z" else 0.02) for m in config.mounts
        ),
    )


def pose(axis="y", angle=90, sha="a" * 64):
    target = [0.0, 0.0, 0.0, math.cos(math.radians(angle) / 2)]
    target["xyz".index(axis)] = math.sin(math.radians(angle) / 2)
    return manual_context("Box", sha, [0, 0, 0, 1], target)


def analyzed_body(tmp_path):
    analysis = analyze_stl(write_box(tmp_path / "box.stl"), density=2700)
    ctx = pose(sha=analysis.sha256)
    return analysis, ctx, orient_body(analysis, ctx)


def test_gravity_and_default_mounts():
    config = ModelConfiguration()
    assert config.gravity[0] == pytest.approx(-9.81 * math.sin(math.radians(2.2)))
    assert config.gravity[1] == pytest.approx(-9.81 * math.cos(math.radians(2.2)) / math.sqrt(2))
    assert config.gravity[1] == config.gravity[2]
    assert np.linalg.norm(config.gravity) == pytest.approx(9.81)
    assert config.outer_width_m == pytest.approx(0.024)
    assert [(m.array_index, m.nozzle_index) for m in config.mounts if m.rotation_axis == "y"] == [
        (2, 1),
        (4, 1),
    ]
    assert [(m.array_index, m.nozzle_index) for m in config.mounts if m.rotation_axis == "z"] == [
        (1, 1),
        (3, 1),
    ]


@pytest.mark.parametrize("axis,angle", [("x", 90), ("y", -90), ("z", 90)])
def test_pose_turn_and_roadmap_priority(axis, angle):
    config = ModelConfiguration()
    assert resolve_turn(pose(axis, angle), config) == Turn(axis, angle)
    ctx = {**pose(), "kind": "roadmap", "actuation": f"free_{axis}", "signed_angle_deg": angle}
    assert resolve_turn(ctx, config) == Turn(axis, angle)
    ctx["commands"] = [{"axis": "x", "signed_angle_deg": -45}]
    assert resolve_turn(ctx, config) == Turn("x", -45)


def test_ambiguous_and_multi_step_turns():
    config = ModelConfiguration()
    with pytest.raises(ValueError, match="mehrdeutig"):
        resolve_turn(pose(angle=180), config)
    assert resolve_turn(
        pose(angle=180), replace(config, manual_axis="y", manual_angle_deg=-180)
    ) == Turn("y", -180)
    with pytest.raises(ValueError, match="Mehrstufige"):
        resolve_turn({**pose(), "flip_count": 2}, config)
    with pytest.raises(ValueError, match="eindeutige"):
        resolve_turn(pose(angle=0), config)
    with pytest.raises(ValueError, match="passen nicht"):
        resolve_turn(pose(angle=180), replace(config, manual_axis="x", manual_angle_deg=180))


@pytest.mark.parametrize(
    "axis,sign,normal,beta,half,vertical",
    [
        ("y", 1, "z", 0, 0.01, 0.02),
        ("y", -1, "z", 0, 0.01, 0.02),
        ("z", 1, "y", 90, 0.01, 0.015),
        ("z", -1, "y", 90, 0.01, 0.015),
        ("x", 1, "y", 90, 0.02, 0.015),
        ("x", -1, "z", 0, 0.015, 0.02),
    ],
)
def test_box_mechanics_independent_reference(tmp_path, axis, sign, normal, beta, half, vertical):
    _, _, body = analyzed_body(tmp_path)
    config = configured(uphill_deg=0, cross_slope_deg=beta)
    mount = NozzleMount(1, 4 if axis == "x" else 1, axis, normal, 0.01)
    result = tipping_mechanics(body, Turn(axis, sign * 90), mount, config)
    assert result.delta_h_m == pytest.approx(math.hypot(half, vertical) - vertical)
    assert result.tipping_angle_deg == pytest.approx(math.degrees(math.atan2(half, vertical)))
    dimensions = [0.02, 0.03, 0.04]
    perpendicular = [dimensions[i] for i in range(3) if i != "xyz".index(axis)]
    expected = body.mass_kg * (
        (perpendicular[0] ** 2 + perpendicular[1] ** 2) / 12 + half**2 + vertical**2
    )
    assert result.inertia_axis_kg_m2 == pytest.approx(expected)


def test_collision_and_missing_straight_edge(tmp_path):
    _, _, body = analyzed_body(tmp_path)
    with pytest.raises(ValueError, match="durchdringt"):
        tipping_mechanics(
            body,
            Turn("x", 90),
            NozzleMount(2, 4, "x", "z", 0.01),
            configured(uphill_deg=0, cross_slope_deg=0),
        )
    vertices = np.array([[0, 0, 0], [0.01, 0.02, 0], [0, 0, 0.03], [0.01, 0.02, 0.03]])
    irregular = replace(body, vertices=vertices)
    with pytest.raises(ValueError, match="Kippkante"):
        tipping_mechanics(irregular, Turn("y", 90), NozzleMount(2, 1, "y", "z", 0.01), configured())


@pytest.mark.parametrize("angle", [90, -90])
def test_offset_conversion_full_hits_and_lever(tmp_path, angle):
    _, _, body = analyzed_body(tmp_path)
    config = configured(uphill_deg=0, cross_slope_deg=0, spread_angle_deg=0)
    mount = next(m for m in config.mounts if m.array_index == 2 and m.nozzle_index == 1)
    turn = Turn("y", angle)
    mechanics = tipping_mechanics(body, turn, mount, config)
    impact = AutomaticOffsetEstimator().estimate(body, turn, mount, mechanics, config)
    assert impact.weights == (1.0,) * 8
    radius = config.jet_radius_m
    for p in impact.points_m:
        assert p[0] == pytest.approx(body.vertices[:, 0].max() + radius - impact.offset_m)
    assert impact.area_eff_m2 == pytest.approx(8 * math.pi * radius**2)
    assert impact.lever_arm_m == pytest.approx(0.02 - radius - config.margin_m, abs=1e-7)
    assert abs(impact.com_offsets_m[0][0]) != pytest.approx(impact.lever_arm_m)
    expected_offset = 0.02 - config.margin_m if angle > 0 else 2 * radius + config.margin_m
    assert impact.offset_m == pytest.approx(expected_offset, abs=1e-7)


def test_partial_width_shift_and_no_hit(tmp_path):
    _, _, body = analyzed_body(tmp_path)
    config = configured(uphill_deg=0, cross_slope_deg=0)
    mount = next(m for m in config.mounts if m.array_index == 2 and m.nozzle_index == 1)
    turn = Turn("y", 90)
    mechanics = tipping_mechanics(body, turn, mount, config)
    estimator = AutomaticOffsetEstimator()
    partial = estimator.estimate(body, turn, mount, mechanics, config)
    assert sum(partial.weights) == 8
    assert 0 < sum(partial.full_weights) < sum(partial.weights)
    one = estimator.estimate(
        body,
        turn,
        replace(mount, row_center_m=-0.005),
        mechanics,
        replace(config, spread_angle_deg=0),
    )
    assert 0 < sum(one.weights) < 8
    single = estimator.estimate(
        body,
        turn,
        replace(mount, row_center_m=0.039),
        mechanics,
        replace(config, spread_angle_deg=0),
    )
    assert sum(single.weights) == 1
    with pytest.raises(ValueError, match="Öffnung"):
        estimator.estimate(body, turn, replace(mount, row_center_m=1), mechanics, config)


def test_three_hit_weights_and_tangency():
    shape = box(0, 0, 10, 10)
    assert jet_hit_weights(shape, [(5, 5), (0, 5), (-0.5, 5), (-1, 5), (-1.1, 5)], 1) == (
        1,
        0.5,
        0,
        0,
        0,
    )
    hollow = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], holes=[[(3, 3), (7, 3), (7, 7), (3, 7)]])
    assert jet_hit_weights(hollow, [(5, 5), (2.5, 5), (3, 5), (3.5, 5), (4, 5)], 1) == (
        0,
        1,
        0.5,
        0,
        0,
    )


def test_coverage_against_circular_segment_reference():
    positions = [-1.1, -1, -0.8, -0.5, -0.4, 0, 0.4, 0.5, 0.8, 1, 1.1]
    centers = [(x, 0) for x in positions]
    shape = box(0, -10, 10, 10)
    expected = [
        0 if x <= -1 else 1 if x >= 1 else 0.5 + (math.asin(x) + x * math.sqrt(1 - x * x)) / math.pi
        for x in positions
    ]
    actual = jet_overlap_fractions(shape, centers, 1)
    assert actual == pytest.approx(expected, abs=1e-5)
    assert jet_hit_weights(shape, centers, 1) == (0, 0, 0, 0, 0.5, 0.5, 0.5, 1, 1, 1, 1)
    # Coverage depends on the relative geometry, not on the import length unit.
    assert jet_overlap_fractions(
        box(0, -0.01, 0.01, 0.01), [(x / 1000, 0) for x in positions], 0.001
    ) == pytest.approx(actual, abs=1e-12)


def test_half_hit_interval_thresholds_and_customization():
    assert coverage_weights((0, 0.249999, 0.25, 0.5, 0.749999, 0.75, 1)) == (
        0,
        0,
        0.5,
        0.5,
        0.5,
        1,
        1,
    )
    shape, centers = box(0, -10, 10, 10), [(-0.2, 0), (0, 0), (0.2, 0)]
    assert jet_hit_weights(shape, centers, 1) == (0.5, 0.5, 0.5)
    assert jet_hit_weights(shape, centers, 1, half_lower=0.4, half_upper=0.6) == (0, 0.5, 1)


@pytest.mark.parametrize(
    "lower,upper",
    [
        (0, 0.75),
        (0.25, 1),
        (0.6, 0.7),
        (0.2, 0.4),
        (0.5, 0.5),
        (float("nan"), 0.75),
        (0.25, float("inf")),
    ],
)
def test_invalid_hit_thresholds_rejected(lower, upper):
    with pytest.raises(ValueError, match="Treffergrenzen"):
        ModelConfiguration(half_hit_lower=lower, half_hit_upper=upper).validate()
    with pytest.raises(ValueError, match="Treffergrenzen"):
        jet_hit_weights(box(0, 0, 1, 1), [(0.5, 0.5)], 1, half_lower=lower, half_upper=upper)


def test_screenshot_regression_seven_hits_one_misses(tmp_path):
    mesh = trimesh.creation.box(extents=[80, 20, 20])
    # A tiny non-cardinal pose reproduces the near-edge placement in the screenshot.
    mesh.apply_transform(trimesh.transformations.rotation_matrix(math.radians(0.004), [0, 0, 1]))
    path = tmp_path / "near_edge.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    analysis = analyze_stl(path, density=1000)
    body = orient_body(analysis, pose("y", -90))
    config = configured(distance_m=0.0174, contact_tolerance_m=0.00005)
    mount = NozzleMount(2, 1, "y", "z", 0.012)
    turn = Turn("y", -90)
    mechanics = tipping_mechanics(body, turn, mount, config)
    impact = AutomaticOffsetEstimator().estimate(body, turn, mount, mechanics, config)
    assert impact.weights == (0.5,) + (1.0,) * 5 + (0.5, 0.0)
    assert len(impact.coverage_fractions) == 8
    assert 0.5 < impact.coverage_fractions[0] < 0.75
    assert 0.25 < impact.coverage_fractions[6] < 0.5
    assert impact.coverage_fractions[7] == 0
    assert sum(impact.full_weights) == 1
    assert impact.offset_m == pytest.approx(0.0078366, abs=1e-6)
    assert impact.area_eff_m2 == pytest.approx(6 * math.pi * config.jet_radius_m**2)
    predictor = AnalyticalPressureModel()
    weighted = predictor.predict(body, mechanics, impact, 100, config)
    binary = predictor.predict(
        body,
        mechanics,
        replace(impact, area_eff_m2=7 * math.pi * config.jet_radius_m**2),
        100,
        config,
    )
    full_only = predictor.predict(
        body,
        mechanics,
        replace(impact, area_eff_m2=sum(impact.full_weights) * math.pi * config.jet_radius_m**2),
        100,
        config,
    )
    assert binary.gauge_pa < weighted.gauge_pa < full_only.gauge_pa


def test_holes_are_excluded_by_exact_projection(tmp_path):
    _, _, body = analyzed_body(tmp_path)
    # Four solid bars make a frame with a through-opening in the XY projection.
    bars = []
    for extents, center in [
        ([0.04, 0.004, 0.01], [0, 0.002, 0.005]),
        ([0.04, 0.004, 0.01], [0, 0.038, 0.005]),
        ([0.004, 0.032, 0.01], [-0.018, 0.02, 0.005]),
        ([0.004, 0.032, 0.01], [0.018, 0.02, 0.005]),
    ]:
        mesh = trimesh.creation.box(extents=extents)
        mesh.apply_translation(center)
        bars.append(mesh)
    frame = trimesh.util.concatenate(bars)
    body = replace(body, vertices=np.asarray(frame.vertices), faces=np.asarray(frame.faces))
    shape = body.silhouette("z")
    from shapely import Point

    assert not shape.covers(Point(0, 0.02))
    assert shape.covers(Point(-0.018, 0.02))
    config = configured(uphill_deg=0, cross_slope_deg=0, spread_angle_deg=0)
    mechanics = TippingMechanics((0.02, 0.04, 0), ((-0.02, 0.04, 0), (0.02, 0.04, 0)), 1, 0.01, 45)
    impact = AutomaticOffsetEstimator().estimate(
        body, Turn("x", -90), NozzleMount(2, 4, "x", "z", 0.02), mechanics, config
    )
    assert sum(impact.weights) == 1  # A bounding box would incorrectly admit all eight.


def test_longitudinal_row_offsets_center_maximal_region(tmp_path):
    _, _, body = analyzed_body(tmp_path)
    config = configured(
        uphill_deg=0, cross_slope_deg=0, spread_angle_deg=0, opening_count=3, pitch_m=0.004
    )
    mount = NozzleMount(2, 4, "x", "z", 0.01)
    mechanics = tipping_mechanics(body, Turn("x", -90), mount, config)
    impact = AutomaticOffsetEstimator().estimate(body, Turn("x", -90), mount, mechanics, config)
    assert sum(impact.weights) == 3
    assert np.diff([p[0] for p in impact.points_m]) == pytest.approx([0.004, 0.004])
    assert np.mean([p[0] for p in impact.points_m]) == pytest.approx(0, abs=1e-8)


@pytest.mark.parametrize("pulse_ms", [10, 100, 500])
def test_pressure_inverse_matches_force_without_clipping(tmp_path, pulse_ms):
    _, _, body = analyzed_body(tmp_path)
    config = configured(uphill_deg=0, cross_slope_deg=0)
    mount = next(m for m in config.mounts if m.array_index == 2 and m.nozzle_index == 1)
    mechanics = tipping_mechanics(body, Turn("y", 90), mount, config)
    impact = AutomaticOffsetEstimator().estimate(body, Turn("y", 90), mount, mechanics, config)
    result = AnalyticalPressureModel().predict(body, mechanics, impact, pulse_ms, config)
    expected_force = math.sqrt(
        2 * mechanics.inertia_axis_kg_m2 * body.mass_kg * 9.81 * mechanics.delta_h_m
    ) / (pulse_ms / 1000 * impact.lever_arm_m)
    assert result.force_n == pytest.approx(expected_force)
    assert drag_force(result.gauge_pa, impact.area_eff_m2, config) == pytest.approx(expected_force)
    assert result.absolute_pa - result.gauge_pa == pytest.approx(config.ambient_pa)
    if pulse_ms == 10:
        assert result.gauge_pa > 80000
        assert result.warnings
    twice_area = replace(impact, area_eff_m2=impact.area_eff_m2 * 2)
    assert (
        AnalyticalPressureModel().predict(body, mechanics, twice_area, pulse_ms, config).gauge_pa
        < result.gauge_pa
    )


def test_candidates_ignore_hardware_activations_and_use_individual_pulses(tmp_path):
    analysis, ctx, _ = analyzed_body(tmp_path)
    params = parameters()
    for array in params["arrays"]:
        array["enabled"] = False
        array["nozzles_enabled"] = [False] * 6
    params["arrays"][3]["pulse_duration_ms"] = 100
    config = configured(uphill_deg=0, cross_slope_deg=0)
    result = AutotunePipeline().prepare(analysis, ctx, params, config)
    assert not result.missing
    assert [(c.mount.array_index, c.mount.nozzle_index) for c in result.candidates] == [
        (2, 1),
        (4, 1),
    ]
    assert all(c.pressure is not None for c in result.candidates)
    assert result.candidates[1].pressure.gauge_pa < result.candidates[0].pressure.gauge_pa
    assert result.ml.status == "not_implemented"
    assert not any(a["enabled"] for a in params["arrays"])


def test_missing_mount_and_parameter_validation(tmp_path):
    analysis, ctx, _ = analyzed_body(tmp_path)
    result = AnalyticalModel().evaluate(analysis, ctx, parameters(), ModelConfiguration())
    assert all(c.status == "blocked" and "Montageposition" in c.message for c in result)
    for values in (
        {"kappa": 1},
        {"pitch_m": 0.0001},
        {"diameter_m": float("nan")},
        {"spread_angle_deg": 180},
        {"margin_m": -1},
        {"opening_count": 0},
    ):
        with pytest.raises(ValueError):
            ModelConfiguration(**values).validate()


def test_tilted_energy_barrier_uses_actual_gravity(tmp_path):
    _, _, body = analyzed_body(tmp_path)
    config = configured()
    mechanics = tipping_mechanics(body, Turn("y", 90), NozzleMount(2, 1, "y", "z", 0.015), config)
    alpha, beta = math.radians(2.2), math.radians(45)
    a = -0.01 * math.sin(alpha) + 0.02 * math.cos(alpha) * math.cos(beta)
    b = 0.02 * math.sin(alpha) + 0.01 * math.cos(alpha) * math.cos(beta)
    assert mechanics.delta_h_m == pytest.approx(math.hypot(a, b) - a)
    assert mechanics.tipping_angle_deg == pytest.approx(math.degrees(math.atan2(b, a)))


def test_rotated_inertia_and_center_are_consistent(tmp_path):
    analysis, _, _ = analyzed_body(tmp_path)
    body = orient_body(analysis, {"source_quaternion_xyzw": [math.sqrt(0.5), 0, 0, math.sqrt(0.5)]})
    assert body.center == pytest.approx([0, 0.02, 0.015])
    original = np.diag(analysis.material.inertia_kg_m2)
    np.testing.assert_allclose(body.inertia, np.diag(original[[0, 2, 1]]), atol=1e-19)


def test_unsymmetric_prism_uses_mass_center_not_bounding_box(tmp_path):
    length, width, height = 20, 30, 40
    vertices = [
        [0, 0, 0],
        [length, 0, 0],
        [0, 0, height],
        [0, width, 0],
        [length, width, 0],
        [0, width, height],
    ]
    faces = [[0, 3, 4], [0, 4, 1], [0, 1, 2], [3, 5, 4], [0, 2, 5], [0, 5, 3], [1, 4, 5], [1, 5, 2]]
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    path = tmp_path / "wedge.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    analysis = analyze_stl(path, density=2700)
    ctx = pose(sha=analysis.sha256)
    body = orient_body(analysis, ctx)
    assert body.center == pytest.approx([0.02 / 3, 0.015, 0.04 / 3])
    config = configured(uphill_deg=0, cross_slope_deg=0, spread_angle_deg=0)
    mount = NozzleMount(2, 1, "y", "z", 0.015)
    mechanics = tipping_mechanics(body, Turn("y", 90), mount, config)
    a, b = 2 * 0.02 / 3, 0.04 / 3
    assert mechanics.delta_h_m == pytest.approx(math.hypot(a, b) - b)
    expected = body.mass_kg * ((0.02**2 + 0.04**2) / 18 + a * a + b * b)
    assert mechanics.inertia_axis_kg_m2 == pytest.approx(expected)
    impact = AutomaticOffsetEstimator().estimate(body, Turn("y", 90), mount, mechanics, config)
    assert impact.center_offsets_m[0] != impact.com_offsets_m[0]


def test_full_mesh_and_pressure_limits(tmp_path):
    mesh = trimesh.creation.box(extents=[20, 30, 40])
    for _ in range(4):
        mesh = mesh.subdivide()
    path = tmp_path / "dense.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    analysis = analyze_stl(path, density=2700)
    assert len(analysis.faces) == 3072
    assert len(analysis.preview_triangles) <= 1500
    assert not analysis.vertices_m.flags.writeable
    assert not analysis.faces.flags.writeable
    body = orient_body(analysis, pose())
    config = configured(uphill_deg=0, cross_slope_deg=0)
    mount = NozzleMount(2, 1, "y", "z", 0.015)
    mechanics = tipping_mechanics(body, Turn("y", 90), mount, config)
    impact = AutomaticOffsetEstimator().estimate(body, Turn("y", 90), mount, mechanics, config)
    pressure = AnalyticalPressureModel().predict(body, mechanics, impact, 1, config)
    assert pressure.gauge_pa > 600000
    assert len(pressure.warnings) == 3
    expected_critical = config.ambient_pa * ((1.2) ** 3.5 - 1)
    assert pressure.critical_gauge_pa == pytest.approx(expected_critical)
    with pytest.raises(ValueError, match="positiv"):
        drag_force(10000, 0, config)
