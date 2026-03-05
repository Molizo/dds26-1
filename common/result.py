"""Structured result types for coordinator → route communication.

These types convey checkout outcomes without leaking protocol details
into HTTP response construction (which belongs in the route layer).
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CheckoutResult:
    success: bool
    # True when the order was already paid; route returns 200 without re-executing
    already_paid: bool = False
    # Human-readable reason for failure; route maps this to a 4xx body
    error: Optional[str] = None
    # Suggested HTTP status code; defaults to 200 on success or 400 on failure
    status_code: int = 200

    @classmethod
    def ok(cls) -> "CheckoutResult":
        return cls(success=True, status_code=200)

    @classmethod
    def paid(cls) -> "CheckoutResult":
        return cls(success=True, already_paid=True, status_code=200)

    @classmethod
    def fail(cls, reason: str, code: int = 400) -> "CheckoutResult":
        return cls(success=False, error=reason, status_code=code)

    @classmethod
    def conflict(cls, reason: str = "Checkout already in progress") -> "CheckoutResult":
        return cls(success=False, error=reason, status_code=409)
