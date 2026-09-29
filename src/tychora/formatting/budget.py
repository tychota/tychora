"""Operational measurement of exact UTF-8 delivery payloads."""

from dataclasses import dataclass

from tychora.ports.tokens import TokenCounter


@dataclass(frozen=True)
class PayloadMeasure:
    tokens: int
    bytes: int


def measure_payloads(
    payloads: tuple[bytes, ...], counter: TokenCounter
) -> PayloadMeasure:
    """Count every delivered representation, including intentional echoes."""
    return PayloadMeasure(
        tokens=sum(counter.count(payload.decode("utf-8")) for payload in payloads),
        bytes=sum(len(payload) for payload in payloads),
    )
