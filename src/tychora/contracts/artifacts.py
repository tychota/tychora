"""Immutable artifact identity shared by corpus verification and byte storage."""

from .base import Contract, NonEmptyStr, NonNegativeInt, Sha256


class ArtifactRef(Contract):
    sha256: Sha256
    size_bytes: NonNegativeInt
    media_type: NonEmptyStr
    storage_key: NonEmptyStr
