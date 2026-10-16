"""Dependency-light CAD pose previews for the pressure-control GUI."""

from __future__ import annotations

import math
import struct
from pathlib import Path

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF

COORDINATE_AXIS_COLORS = {"X": "#ef4444", "Y": "#22c55e", "Z": "#3b82f6"}

Vector = tuple[float, float, float]
Matrix = tuple[Vector, Vector, Vector]
Triangle = tuple[Vector, Vector, Vector]


def _load_triangles(path: Path) -> tuple[Triangle, ...]:
    source = Path(path)
    if source.suffix.lower() == ".stl":
        data = source.read_bytes()
        if len(data) >= 84:
            count = struct.unpack_from("<I", data, 80)[0]
            if 84 + count * 50 == len(data):
                result = []
                for index in range(count):
                    values = struct.unpack_from("<12fH", data, 84 + index * 50)
                    result.append(
                        tuple(
                            tuple(float(value) for value in values[start : start + 3])
                            for start in (3, 6, 9)
                        )
                    )
                return tuple(result)  # type: ignore[return-value]
        vertices: list[Vector] = []
        for line in data.decode("utf-8", errors="ignore").splitlines():
            fields = line.strip().split()
            if len(fields) == 4 and fields[0].lower() == "vertex":
                vertices.append(tuple(float(value) for value in fields[1:]))  # type: ignore[arg-type]
        if vertices and len(vertices) % 3 == 0:
            return tuple(
                (vertices[index], vertices[index + 1], vertices[index + 2])
                for index in range(0, len(vertices), 3)
            )
        raise ValueError(f"STL does not contain readable triangles: {source}")

    if source.suffix.lower() == ".obj":
        vertices = []
        faces: list[tuple[int, int, int]] = []
        for line in source.read_text(encoding="utf-8", errors="ignore").splitlines():
            fields = line.strip().split()
            if len(fields) >= 4 and fields[0] == "v":
                vertices.append(tuple(float(value) for value in fields[1:4]))  # type: ignore[arg-type]
            elif len(fields) >= 4 and fields[0] == "f":
                indices = [int(value.split("/", 1)[0]) - 1 for value in fields[1:]]
                for index in range(1, len(indices) - 1):
                    faces.append((indices[0], indices[index], indices[index + 1]))
        if vertices and faces:
            return tuple(tuple(vertices[index] for index in face) for face in faces)  # type: ignore[return-value]
        raise ValueError(f"OBJ does not contain readable triangles: {source}")

    raise ValueError(f"Unsupported 3D format: {source.suffix}")


def _transform(point: Vector, matrix: Matrix) -> Vector:
    return tuple(
        sum(matrix[row][column] * point[column] for column in range(3)) for row in range(3)
    )  # type: ignore[return-value]


def _quaternion_matrix(quaternion: tuple[float, float, float, float]) -> Matrix:
    x, y, z, w = quaternion
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def _view_matrix() -> Matrix:
    """Roadmap view: +X down-left/downhill, +Y down-right, and +Z up."""
    horizontal = 1.0 / math.sqrt(2.0)
    vertical = 1.0 / math.sqrt(6.0)
    depth = 1.0 / math.sqrt(3.0)
    return (
        (-horizontal, horizontal, 0.0),
        (-vertical, -vertical, 2.0 * vertical),
        (-depth, -depth, -depth),
    )


def _draw_axes(painter: QPainter, width: int, height: int, view: Matrix) -> None:
    origin = (width - 42.0, 39.0)
    axis_length = max(13.0, min(25.0, width * 0.11, height * 0.16))
    axes = (
        ("X", (1.0, 0.0, 0.0)),
        ("Z", (0.0, 0.0, 1.0)),
        ("Y", (0.0, 1.0, 0.0)),
    )
    for label, axis in axes:
        transformed = _transform(axis, view)
        direction = (transformed[0], -transformed[1])
        length = math.hypot(*direction)
        if length < 1e-9:
            color = QColor(COORDINATE_AXIS_COLORS[label])
            painter.setPen(QPen(color, 2.0))
            painter.setBrush(QColor("#111827"))
            painter.drawEllipse(QPointF(*origin), 6.0, 6.0)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(*origin), 2.0, 2.0)
            painter.drawText(QPointF(origin[0] - 15.0, origin[1] + 5.0), label)
            continue
        unit = (direction[0] / length, direction[1] / length)
        end = (origin[0] + unit[0] * axis_length, origin[1] + unit[1] * axis_length)
        perpendicular = (-unit[1], unit[0])
        color = QColor(COORDINATE_AXIS_COLORS[label])
        painter.setPen(QPen(color, 2.2))
        painter.drawLine(QPointF(*origin), QPointF(*end))
        painter.setBrush(color)
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(*end),
                    QPointF(
                        end[0] - unit[0] * 6.0 + perpendicular[0] * 3.0,
                        end[1] - unit[1] * 6.0 + perpendicular[1] * 3.0,
                    ),
                    QPointF(
                        end[0] - unit[0] * 6.0 - perpendicular[0] * 3.0,
                        end[1] - unit[1] * 6.0 - perpendicular[1] * 3.0,
                    ),
                ]
            )
        )
        painter.drawText(
            QPointF(end[0] + unit[0] * 8.0, end[1] + unit[1] * 8.0 + 4.0),
            label,
        )


def _polygon(points: tuple[Vector, ...], project) -> QPolygonF:
    return QPolygonF([QPointF(*project(point)) for point in points])


def render_mesh_preview(
    path: Path,
    width: int,
    height: int,
    *,
    quaternion_xyzw: tuple[float, float, float, float] | None = None,
    caption: str = "Target orientation · model view",
) -> QPixmap:
    triangles = _load_triangles(path)
    points = [point for triangle in triangles for point in triangle]
    minimum = tuple(min(point[axis] for point in points) for axis in range(3))
    maximum = tuple(max(point[axis] for point in points) for axis in range(3))
    center = tuple((minimum[axis] + maximum[axis]) * 0.5 for axis in range(3))
    orientation = _quaternion_matrix(quaternion_xyzw or (0.0, 0.0, 0.0, 1.0))
    oriented = [
        tuple(
            _transform(
                tuple(point[axis] - center[axis] for axis in range(3)),  # type: ignore[arg-type]
                orientation,
            )
            for point in triangle
        )
        for triangle in triangles
    ]
    oriented_points = [point for triangle in oriented for point in triangle]
    shift_y = -min(point[1] for point in oriented_points)
    shift_z = -min(point[2] for point in oriented_points)
    oriented = [
        tuple((point[0], point[1] + shift_y, point[2] + shift_z) for point in triangle)
        for triangle in oriented
    ]
    oriented_points = [point for triangle in oriented for point in triangle]
    minimum = tuple(min(point[axis] for point in oriented_points) for axis in range(3))
    maximum = tuple(max(point[axis] for point in oriented_points) for axis in range(3))
    span_3d = tuple(max(maximum[axis] - minimum[axis], 1e-9) for axis in range(3))
    margin = 0.12 * max(span_3d)
    x0, x1 = minimum[0] - margin, maximum[0] + margin
    y1, z1 = maximum[1] + margin, maximum[2] + margin
    floor: tuple[Vector, ...] = (
        (x0, 0.0, 0.0),
        (x1, 0.0, 0.0),
        (x1, y1, 0.0),
        (x0, y1, 0.0),
    )
    wall: tuple[Vector, ...] = (
        (x0, 0.0, 0.0),
        (x1, 0.0, 0.0),
        (x1, 0.0, z1),
        (x0, 0.0, z1),
    )
    view = _view_matrix()
    camera_triangles = [
        tuple(_transform(point, view) for point in triangle) for triangle in oriented
    ]
    camera_floor = tuple(_transform(point, view) for point in floor)
    camera_wall = tuple(_transform(point, view) for point in wall)
    camera_points = [
        point for group in (*camera_triangles, camera_floor, camera_wall) for point in group
    ]
    projected = [(point[0], -point[1]) for point in camera_points]
    min_2d = tuple(min(point[axis] for point in projected) for axis in range(2))
    max_2d = tuple(max(point[axis] for point in projected) for axis in range(2))
    span_2d = tuple(max(max_2d[axis] - min_2d[axis], 1e-9) for axis in range(2))
    drawing_height = max(1, height - 22)
    scale = min((width - 24) / span_2d[0], (drawing_height - 14) / span_2d[1])
    scene_center = tuple((min_2d[axis] + max_2d[axis]) * 0.5 for axis in range(2))

    def project(point: Vector) -> tuple[float, float]:
        return (
            (point[0] - scene_center[0]) * scale + width * 0.5,
            (-point[1] - scene_center[1]) * scale + drawing_height * 0.5 + 2.0,
        )

    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#111827"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#9a7548"), 0.8))
    painter.setBrush(QColor(227, 201, 168, 70))
    painter.drawPolygon(_polygon(camera_wall, project))
    painter.setPen(QPen(QColor("#7d8590"), 0.8))
    painter.setBrush(QColor(201, 209, 217, 62))
    painter.drawPolygon(_polygon(camera_floor, project))
    painter.setPen(QPen(QColor("#cbd5e1"), 1.5))
    painter.drawLine(QPointF(*project(camera_wall[0])), QPointF(*project(camera_wall[1])))

    def depth(triangle: tuple[Vector, ...]) -> float:
        return sum(point[2] for point in triangle) / 3.0

    for triangle in sorted(camera_triangles, key=depth):
        first = tuple(triangle[1][axis] - triangle[0][axis] for axis in range(3))
        second = tuple(triangle[2][axis] - triangle[0][axis] for axis in range(3))
        normal = (
            first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0],
        )
        length = math.sqrt(sum(value * value for value in normal))
        light_dot = sum(
            value * light for value, light in zip(normal, (-0.35, -0.45, 0.82), strict=True)
        )
        brightness = 0.55 if length == 0 else 0.42 + 0.48 * abs(light_dot / length)
        base = tuple(max(0, min(255, int(value * brightness))) for value in (56, 189, 248))
        painter.setBrush(QColor(*base))
        painter.setPen(QPen(QColor("#164e63"), 0.7))
        painter.drawPolygon(_polygon(triangle, project))

    _draw_axes(painter, width, height, view)
    painter.setPen(QPen(QColor("#94a3b8"), 1.0))
    painter.drawText(8, height - 8, caption)
    painter.end()
    return pixmap
