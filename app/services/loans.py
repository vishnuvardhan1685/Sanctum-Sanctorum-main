"""Library loan operations: borrowing and returning books."""
from datetime import datetime, timedelta
from math import ceil
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Loan, MemberTier
from app.schemas import LoanCreate, LoanOut, LoanStatus
from app.services import books as book_service
from app.services import members as member_service

# Maximum concurrent unreturned loans per tier (None = unlimited).
TIER_LOAN_LIMIT: Dict[str, Optional[int]] = {
    MemberTier.APPRENTICE.value: 1,
    MemberTier.ADEPT.value: 3,
    MemberTier.MASTER.value: 5,
    MemberTier.SUPREME.value: None,
}

LOAN_PERIOD = timedelta(days=14)
LATE_FEE_PER_DAY_CENTS = 25
SECONDS_PER_DAY = 24 * 60 * 60


def loan_status(loan: Loan, now: datetime) -> LoanStatus:
    """``returned`` if returned; else ``overdue`` if now > due_at; else ``active``."""
    if loan.returned_at is not None:
        return "returned"
    return "overdue" if loan.is_overdue(now) else "active"


def to_loan_out(loan: Loan, now: datetime) -> LoanOut:
    """Serialize a loan, computing its status at read time.

    ``status`` is derived from the clock rather than stored, so it cannot go stale between the
    due date passing and the next write.  That is why loans are the one resource whose service
    returns a schema instead of the ORM row.
    """
    return LoanOut(
        id=loan.id,
        member_id=loan.member_id,
        book_id=loan.book_id,
        borrowed_at=loan.borrowed_at,
        due_at=loan.due_at,
        returned_at=loan.returned_at,
        late_fee_cents=loan.late_fee_cents,
        status=loan_status(loan, now),
    )


def calculate_late_fee(due_at: datetime, returned_at: datetime, price_cents: int) -> int:
    """25 cents per started day late (any partial day counts), capped at the book's price; 0 if not late."""
    if returned_at <= due_at:
        return 0
    days_late = ceil((returned_at - due_at).total_seconds() / SECONDS_PER_DAY)
    return min(days_late * LATE_FEE_PER_DAY_CENTS, price_cents)


def create_loan(db: Session, data: LoanCreate, now: datetime) -> LoanOut:
    """Borrow a book for 14 days.

    Checks, in order:
    1. 404 member not found; 404 book not found
    2. 403 book restricted and member tier below master
    3. 409 member has any overdue loan
    4. 409 member already has an unreturned loan of this book
    5. 409 member is at their tier's loan limit
    6. 409 book is out of stock
    On success: borrowed_at = now, due_at = now + 14 days, returned_at None,
    late_fee_cents 0, and stock is decremented by one.
    """
    member = member_service.get_member(db, data.member_id)
    book = book_service.get_book(db, data.book_id)

    if book.restricted:
        member_service.ensure_can_access_restricted(member)

    # Checks 3, 4 and 5 all ask about the member's open loans, so they are loaded once.
    open_loans = db.scalars(
        select(Loan).where(Loan.member_id == member.id, Loan.returned_at.is_(None))
    ).all()

    if any(loan.is_overdue(now) for loan in open_loans):
        raise HTTPException(status_code=409, detail="Member has an overdue loan to return first")
    if any(loan.book_id == book.id for loan in open_loans):
        raise HTTPException(status_code=409, detail="Member already has this book on loan")

    limit = TIER_LOAN_LIMIT[member.tier]
    if limit is not None and len(open_loans) >= limit:
        raise HTTPException(
            status_code=409, detail=f"Tier '{member.tier}' allows at most {limit} concurrent loans"
        )

    try:
        if not book_service.reserve_stock(db, book.id, 1):
            raise HTTPException(status_code=409, detail="No copies of this book are available")
        loan = Loan(
            member_id=member.id,
            book_id=book.id,
            borrowed_at=now,
            due_at=now + LOAN_PERIOD,
            returned_at=None,
            late_fee_cents=0,
        )
        db.add(loan)
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(loan)
    return to_loan_out(loan, now)


def _get_loan(db: Session, loan_id: int) -> Loan:
    """Fetch a loan row, or raise 404."""
    loan = db.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status_code=404, detail="Loan not found")
    return loan


def get_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a loan by id, or raise 404."""
    return to_loan_out(_get_loan(db, loan_id), now)


def return_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a borrowed book.

    Rules: 404 if missing; 409 if already returned. Sets returned_at = now, restores one copy
    of stock and charges a late fee (see ``calculate_late_fee``).
    """
    loan = _get_loan(db, loan_id)
    if loan.returned_at is not None:
        raise HTTPException(status_code=409, detail="This loan has already been returned")

    book = book_service.get_book(db, loan.book_id)
    try:
        loan.returned_at = now
        loan.late_fee_cents = calculate_late_fee(
            loan.due_at,
            now,
            book.price_cents,
        )
        book_service.release_stock(db, loan.book_id, 1)
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(loan)
    return to_loan_out(loan, now)


def list_member_loans(
    db: Session, member_id: int, now: datetime, status: Optional[LoanStatus] = None
) -> List[LoanOut]:
    """A member's loans ordered by id, optionally filtered by computed status; 404 if member missing."""
    member_service.get_member(db, member_id)
    loans = db.scalars(select(Loan).where(Loan.member_id == member_id).order_by(Loan.id.asc())).all()
    # Status is computed, not a column, so the filter is applied after serialization rather than
    # rebuilt as a second set of SQL predicates that could drift from ``loan_status``.
    serialized = [to_loan_out(loan, now) for loan in loans]
    if status is None:
        return serialized
    return [loan for loan in serialized if loan.status == status]