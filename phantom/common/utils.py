import asyncio
import math
from collections.abc import Callable, Iterable
from dataclasses import fields
from functools import cache, wraps
from typing import TypeAlias

import numpy as np
import skimage.draw
from sc2.dicts.unit_research_abilities import RESEARCH_INFO
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sklearn.metrics import pairwise_distances as pairwise_distances_sklearn

CVXPY_OPTIONS = dict(
    solver="ECOS",
    # verbose=True,
)

LINPROG_OPTIONS = dict(
    method="highs",
    # bounds=(0.0, None),
    # options=dict(
    #     # maxiter=100,
    #     disp=False,
    #     presolve=False,
    #     time_limit=10e-3,
    #     primal_feasibility_tolerance=1e-3,  # default: 1e-7
    #     dual_feasibility_tolerance=1e-3,  # default: 1e-7
    #     ipm_optimality_tolerance=1e-5,  # default: 1e-12
    # ),
    # x0=None,
    # integrality=0,
)

RNG = np.random.default_rng(42)


class PlacementNotFoundException(Exception):
    pass


Point = tuple[int, int]


def async_command(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


def dataclass_from_dict(cls: type, parameters: dict[str, float]):
    field_names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in parameters.items() if k in field_names})


def unit_value(u: Unit, d: np.ndarray) -> float:
    return pow(u.health + u.shield, d[u.position.rounded]) * max(u.ground_dps, u.air_dps)


def can_attack(unit: Unit, target: Unit) -> bool:
    if target.is_cloaked and not target.is_revealed:
        return False
    # elif target.is_burrowed and not any(self.units_detecting(target)):
    #     return False
    elif target.is_flying:
        return unit.can_attack_air
    else:
        return unit.can_attack_ground


def pairwise_distances(a, b):
    if not any(a) or not any(b):
        return np.array([])
    return pairwise_distances_sklearn(a, b, ensure_all_finite=False)


def project_point_onto_line(origin: Point2, direction: Point2, position: Point2) -> Point2:
    orthogonal_direction = Point2((direction[1], -direction[0]))
    return (
        position
        - np.dot(position - origin, orthogonal_direction)
        / np.dot(orthogonal_direction, orthogonal_direction)
        * orthogonal_direction
    )


def get_intersections(position1: Point2, radius1: float, position2: Point2, radius2: float) -> Iterable[Point2]:
    p01 = position2 - position1
    distance = np.linalg.norm(p01)
    if distance > 0 and abs(radius1 - radius2) <= distance <= radius1 + radius2:
        disc = (radius1**2 - radius2**2 + distance**2) / (2 * distance)
        height = math.sqrt(radius1**2 - disc**2)
        middle = position1 + (disc / distance) * p01
        orthogonal = (height / distance) * np.array([p01.y, -p01.x])
        yield middle + orthogonal
        yield middle - orthogonal


def center(points: Iterable[Point]) -> Point2:
    x_sum = 0.0
    y_sum = 0.0
    num_points = 0
    for point in points:
        x_sum += point[0]
        y_sum += point[1]
        num_points += 1
    x_sum /= num_points
    y_sum /= num_points
    return Point2((x_sum, y_sum))


def line(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    lx, ly = skimage.draw.line(x0, y0, x1, y1)
    return [(int(x), int(y)) for x, y in zip(lx, ly, strict=False)]


def circle_perimeter(x0: int, y0: int, r: int, shape: tuple) -> list[tuple[int, int]]:
    assert len(shape) == 2
    tx, ty = skimage.draw.circle_perimeter(x0, y0, r, shape=shape)
    return [(int(x), int(y)) for x, y in zip(tx, ty, strict=False)]


def circle(x0: int, y0: int, r: int, shape: tuple) -> list[tuple[int, int]]:
    assert len(shape) == 2
    tx, ty = skimage.draw.ellipse(x0, y0, r, r, shape=shape)
    return [(int(x), int(y)) for x, y in zip(tx, ty, strict=False)]


def rectangle(start: tuple[int, int], extent: tuple[int, int], shape: tuple) -> tuple[np.ndarray, np.ndarray]:
    assert len(shape) == 2
    rx, ry = skimage.draw.rectangle(start, extent=extent, shape=shape)
    return rx.astype(int).flatten(), ry.astype(int).flatten()


def get_requirements(item: UnitTypeId | UpgradeId) -> Iterable[UnitTypeId | UpgradeId]:
    if isinstance(item, UnitTypeId):
        trainers = UNIT_TRAINED_FROM[item]
        trainer = sorted(trainers, key=lambda v: v.value)[0]
        yield trainer
        info = TRAIN_INFO[trainer][item]
    elif isinstance(item, UpgradeId):
        researcher = UPGRADE_RESEARCHED_FROM[item]
        yield researcher
        info = RESEARCH_INFO[researcher][item]
    else:
        raise TypeError()

    requirements = {info.get("required_building"), info.get("required_upgrade")}
    requirements.discard(None)

    for requirement1 in requirements:
        yield requirement1
        yield from get_requirements(requirement1)


FLOOD_FILL_OFFSETS = {
    Point2((-1, 0)),
    Point2((0, -1)),
    Point2((0, +1)),
    Point2((+1, 0)),
    # Point2((-1, -1)),
    # Point2((-1, +1)),
    # Point2((+1, -1)),
    # Point2((+1, +1)),
}


@cache
def disk(radius: float) -> tuple[np.ndarray, np.ndarray]:
    r = int(radius + 0.5)
    p = radius, radius
    n = 2 * r + 1
    dx, dy = skimage.draw.disk(center=p, radius=radius, shape=(n, n))
    return dx - r, dy - r


def combine_comparers[T](fns: list[Callable[[T, T], int]]) -> Callable[[T, T], int]:
    def combined(a, b):
        for f in fns:
            r = f(a, b)
            if r != 0:
                return r
        return 0

    return combined


def points_of_structure(s: Unit) -> list[Point]:
    dx, dy = disk(s.radius)
    px, py = s.position.rounded
    dx += px
    dy += py
    return list(zip(dx, dy, strict=False))


def logit_to_probability(x: float):
    return 1 / (1 + math.exp(-x))


MacroId: TypeAlias = UnitTypeId | UpgradeId


def calculate_dps(u: Unit, v: Unit) -> float:
    if dps := DPS_OVERRIDE.get(u.type_id):
        return dps
    if not can_attack(u, v):
        return 0.0
    return u.air_dps if v.is_flying else u.ground_dps


DPS_OVERRIDE = {
    UnitTypeId.BUNKER: 40,
    UnitTypeId.PLANETARYFORTRESS: 5,
    UnitTypeId.BANELING: 20,
}
