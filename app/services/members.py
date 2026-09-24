"""Member operations and tier helpers."""

from sqlalchemy.exc import IntegrityError
from datetime import datetime
from typing import List

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Loan, Member, MemberTier, Order, OrderStatus
from app.schemas import MemberCreate, MemberPage, MemberStats, MemberOut

# Tiers from lowest to highest; a member's rank is their index in this list.
TIER_ORDER: List[str] = [
    MemberTier.APPRENTICE.value,
    MemberTier.ADEPT.value,
    MemberTier.MASTER.value,
    MemberTier.SUPREME.value,
]

# Minimum tier allowed to buy or borrow restricted books.
RESTRICTED_MIN_TIER = MemberTier.MASTER.value


def tier_at_least(tier: str, minimum: str) -> bool:
    """True if ``tier`` ranks at or above ``minimum``."""
    return TIER_ORDER.index(tier) >= TIER_ORDER.index(minimum)


def ensure_can_access_restricted(member: Member) -> None:
    """Raise 403 unless the member's tier may access restricted books."""
    if not tier_at_least(member.tier, RESTRICTED_MIN_TIER):
        raise HTTPException(
            status_code=403,
            detail=f"Restricted books require tier '{RESTRICTED_MIN_TIER}' or higher",
        )


def create_member(db: Session, data: MemberCreate, now: datetime) -> Member:
    """Register a member.

    Rules: email (already stripped + lowercased) must be unique -> 409; created_at = now.
    """
    member = Member(name=data.name, email=data.email, tier=data.tier.value, created_at=now)
    db.add(member)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="A member with this email already exists"
        )
    db.refresh(member)
    return member

def get_member(db: Session, member_id: int) -> Member:
    """Return a member by id, or raise 404."""
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


def list_members(db: Session, limit: int = 20, offset: int = 0) -> MemberPage:
    """The member directory, id ascending, paginated the same way the book catalogue is."""
    total = db.scalar(select(func.count()).select_from(Member)) or 0
    members = db.scalars(
        select(Member).order_by(Member.id.asc()).limit(limit).offset(offset)
    ).all()
    member_items = [MemberOut.model_validate(member) for member in members]
    return MemberPage(items=member_items, total=total, limit=limit, offset=offset)


def list_member_orders(db: Session, member_id: int) -> List[Order]:
    """All orders of a member ordered by id ascending; 404 if the member is missing."""
    get_member(db, member_id)
    return list(
        db.scalars(select(Order).where(Order.member_id == member_id).order_by(Order.id))
    )


def get_member_stats(db: Session, member_id: int, now: datetime) -> MemberStats:
    """Summarize a member's activity.

    Rules:
    - 404 if the member is missing.
    - orders_paid / total_spent_cents consider only ``paid`` orders.
    - active_loans counts every unreturned loan (overdue ones included).
    - overdue_loans counts unreturned loans with now > due_at.
    - late_fees_cents sums late fees of returned loans.
    """
    get_member(db, member_id)

    orders_paid, total_spent_cents = db.execute(
        select(
            func.count(Order.id), func.coalesce(func.sum(Order.total_cents), 0)
        ).where(Order.member_id == member_id, Order.status == OrderStatus.PAID.value)
    ).one()

    # Loans are fetched rather than aggregated in SQL so that the overdue boundary stays defined
    # in exactly one place (``Loan.is_overdue``).  One member's loans are a bounded set.
    loans = db.scalars(select(Loan).where(Loan.member_id == member_id)).all()

    return MemberStats(
        member_id=member_id,
        orders_paid=orders_paid,
        total_spent_cents=total_spent_cents,
        active_loans=sum(1 for loan in loans if loan.returned_at is None),
        overdue_loans=sum(1 for loan in loans if loan.is_overdue(now)),
        late_fees_cents=sum(
            loan.late_fee_cents for loan in loans if loan.returned_at is not None
        ),
    )
