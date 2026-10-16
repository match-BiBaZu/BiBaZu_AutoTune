"""Display interpolation for exported orientation segments; no feasibility analysis."""

import math


def multiply(a, b):
    x, y, z, w = a
    X, Y, Z, W = b
    return (
        w * X + x * W + y * Z - z * Y,
        w * Y - x * Z + y * W + z * X,
        w * Z + x * Y - y * X + z * W,
        w * W - x * X - y * Y - z * Z,
    )


def rotate(q, axis, angle):
    half = math.radians(angle) / 2
    vector = [0.0, 0.0, 0.0, math.cos(half)]
    vector["xyz".index(axis)] = math.sin(half)
    return multiply(vector, q)


def command_frame(commands, progress):
    """Interpolate along the specified axes, rather than an endpoint SLERP."""
    if not commands:
        return None
    scaled = min(1.0, max(0.0, progress)) * len(commands)
    index = min(int(scaled), len(commands) - 1)
    command = commands[index]
    return rotate(
        command["start_quaternion_xyzw"],
        command["axis"],
        command["signed_angle_deg"] * min(1.0, scaled - index),
    )


def preview_frame(segments, progress):
    if not segments:
        return None
    position = min(1.0, max(0.0, progress)) * len(segments)
    index = min(int(position), len(segments) - 1)
    segment = segments[index]
    fraction = min(1.0, position - index)
    if segment["segment_kind"] == "command":
        return rotate(
            segment["start_quaternion_xyzw"],
            segment["axis"],
            segment["signed_angle_deg"] * fraction,
        )
    vector = segment["rotation_vector_chute_rad"]
    angle = math.sqrt(sum(float(v) ** 2 for v in vector))
    if angle < 1e-15:
        return segment["start_quaternion_xyzw"]
    rotation = tuple(float(v) / angle * math.sin(angle * fraction / 2) for v in vector) + (
        math.cos(angle * fraction / 2),
    )
    return multiply(rotation, segment["start_quaternion_xyzw"])
