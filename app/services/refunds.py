"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.
"""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from ..models import RefundLog


def log_refund(db: Session, booking_id: int, price_cents: int, percent: int) -> RefundLog:
    # Refund amount rounds to the nearest cent, half-cents rounding up.
    amount_cents = int(
        (Decimal(price_cents) * Decimal(percent) / Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    entry = RefundLog(
        booking_id=booking_id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
