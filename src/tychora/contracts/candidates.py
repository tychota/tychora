"""Shared typed ranking owner identity."""

from typing import Literal

from .base import Identifier

type CandidateKey = tuple[Literal["unit", "asset"], Identifier]
