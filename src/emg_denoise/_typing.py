"""Shared NumPy types and the minimal input protocol for feature extraction."""

from __future__ import annotations

from typing import Protocol, TypeAlias

import numpy as np
from numpy.typing import NDArray

FloatArray: TypeAlias = NDArray[np.float64]
BoolArray: TypeAlias = NDArray[np.bool_]
IntArray: TypeAlias = NDArray[np.int64]
WaveletOutput: TypeAlias = tuple[FloatArray, FloatArray, BoolArray]


class SignalBlock(Protocol):
    """Read-only projection of a denoised block, without a dependency on core."""

    @property
    def samples(self) -> FloatArray: ...

    @property
    def timestamps(self) -> FloatArray: ...

    @property
    def valid(self) -> BoolArray: ...

    @property
    def discontinuity(self) -> bool: ...
