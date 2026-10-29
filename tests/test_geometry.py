import struct

import numpy as np
import pytest
import trimesh

from bibazu_autotune.geometry import analyze_stl, material_properties


def write_box(path, *, ascii=False, translation=(0, 0, 0)):
    mesh = trimesh.creation.box(extents=[20, 30, 40])
    mesh.apply_translation(translation)
    if ascii:
        path.write_text(trimesh.exchange.stl.export_stl_ascii(mesh), "utf-8")
    else:
        path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    return path


@pytest.mark.parametrize("ascii", [False, True])
@pytest.mark.parametrize("unit,scale", [("mm", 0.001), ("cm", 0.01), ("m", 1)])
def test_box_integrals_and_units(tmp_path, ascii, unit, scale):
    result = analyze_stl(write_box(tmp_path / "box.stl", ascii=ascii), unit, 2700)
    g = result.geometry
    dimensions = np.array([20, 30, 40]) * scale
    x, y, z = dimensions
    volume = x * y * z
    expected_inertia = np.diag([y * y + z * z, x * x + z * z, x * x + y * y]) * volume / 12
    assert g.valid_volume
    assert g.dimensions_m == pytest.approx(dimensions)
    assert g.area_m2 == pytest.approx(2 * (x * y + x * z + y * z))
    assert g.volume_m3 == pytest.approx(volume)
    assert g.center_m == pytest.approx([0, 0, 0], abs=1e-12)
    np.testing.assert_allclose(g.inertia_m5, expected_inertia, atol=1e-16)
    assert result.material.mass_kg == pytest.approx(volume * 2700)
    np.testing.assert_allclose(result.material.inertia_kg_m2, expected_inertia * 2700)
    assert len(result.sha256) == 64
    assert g.triangle_count == 12


def test_translation_keeps_centroid_tensor(tmp_path):
    original = analyze_stl(write_box(tmp_path / "original.stl"))
    translated = analyze_stl(write_box(tmp_path / "shift.stl", translation=(100, -200, 300)))
    assert translated.geometry.center_m == pytest.approx([0.1, -0.2, 0.3])
    np.testing.assert_allclose(
        original.geometry.inertia_m5, translated.geometry.inertia_m5, atol=1e-22
    )
    assert original.sha256 != translated.sha256


@pytest.mark.parametrize("damage", ["open", "inconsistent", "inverted"])
def test_invalid_volume_only_exposes_surface_properties(tmp_path, damage):
    mesh = trimesh.creation.box(extents=[20, 30, 40])
    if damage == "open":
        mesh.update_faces(np.arange(len(mesh.faces)) != 0)
    elif damage == "inconsistent":
        mesh.faces[0] = mesh.faces[0][::-1]
    else:
        mesh.invert()
    path = tmp_path / "bad.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    result = analyze_stl(path, density=1000)
    assert result.geometry.area_m2 > 0
    assert not result.geometry.valid_volume
    assert result.geometry.volume_m3 is None
    assert result.geometry.center_m is None
    assert result.geometry.inertia_m5 is None
    assert result.material.mass_kg is None
    assert result.material.inertia_kg_m2 is None
    assert result.warning


@pytest.mark.parametrize("density", [0, -1, float("inf"), float("nan"), True, "2700"])
def test_invalid_density(tmp_path, density):
    g = analyze_stl(write_box(tmp_path / "box.stl")).geometry
    with pytest.raises(ValueError, match="Dichte"):
        material_properties(g, density)


def test_optional_density(tmp_path):
    result = analyze_stl(write_box(tmp_path / "box.stl"))
    assert result.geometry.inertia_m5 is not None
    assert result.material.density_kg_m3 is None
    assert result.material.mass_kg is None


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf")])
def test_nonfinite_vertices_rejected_before_mesh_processing(tmp_path, coordinate):
    path = write_box(tmp_path / "nonfinite.stl")
    data = bytearray(path.read_bytes())
    struct.pack_into("<f", data, 84 + 12, coordinate)
    path.write_bytes(data)
    with pytest.raises(ValueError, match="Koordinaten"):
        analyze_stl(path)


def test_missing_empty_and_wrong_format(tmp_path):
    with pytest.raises(FileNotFoundError):
        analyze_stl(tmp_path / "missing.stl")
    empty = tmp_path / "empty.stl"
    empty.write_bytes(bytes(80) + struct.pack("<I", 0))
    with pytest.raises(ValueError):
        analyze_stl(empty)
    with pytest.raises(ValueError, match="Einheit"):
        analyze_stl(empty, "inch")
    with pytest.raises(ValueError, match="STL"):
        analyze_stl(tmp_path / "box.obj")
