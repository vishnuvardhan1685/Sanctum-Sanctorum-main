"""Order operations: placing, paying and cancelling purchases."""

from datetime import datetime
from typing import Dict, List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Book, Member, MemberTier, Order, OrderItem, OrderStatus
from app.schemas import OrderCreate
from app.services import books as book_service
from app.services import members as member_service

# Percentage discount granted by each membership tier.
TIER_DISCOUNT_PERCENT: Dict[str, int] = {
    MemberTier.APPRENTICE.value: 0,
    MemberTier.ADEPT.value: 5,
    MemberTier.MASTER.value: 10,
    MemberTier.SUPREME.value: 15,
}

# Extra discount when the total quantity across all items reaches the threshold.
BULK_QUANTITY_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5


def calculate_discount_percent(member: Member, total_quantity: int) -> int:
    """Tier discount, plus the bulk discount when total quantity >= threshold."""
    percent = TIER_DISCOUNT_PERCENT[member.tier]
    if total_quantity >= BULK_QUANTITY_THRESHOLD:
        percent += BULK_DISCOUNT_PERCENT
    return percent


def create_order(db: Session, data: OrderCreate, now: datetime) -> Order:
    """Place a pending order and reserve stock.

    Checks, in order (422 for empty items / bad quantity / duplicate books is done by the schema):
    1. 404 member not found; 404 any book not found
    2. 403 any book restricted and member tier below master
    3. 409 any book has insufficient stock (all-or-nothing: nothing is changed)
    Then stock is decremented for every item and prices are snapshotted.
    Pricing: discount_cents = subtotal * percent // 100; total = subtotal - discount.
    """
    member = member_service.get_member(db, data.member_id)

    # Every book is resolved before any other rule runs, so a missing book reports 404 even when
    # an earlier line would also have failed the tier check.
    books: Dict[int, Book] = {
        item.book_id: book_service.get_book(db, item.book_id) for item in data.items
    }

    if any(book.restricted for book in books.values()):
        member_service.ensure_can_access_restricted(member)

    try:
        for item in data.items:
            if not book_service.reserve_stock(db, item.book_id, item.quantity):
                raise HTTPException(
                    status_code=409,
                    detail=f"Insufficient stock for book {item.book_id}",
                )
        order = _build_order(member, books, data, now)
        db.add(order)
        db.commit()
    except Exception:
        # Any failure discards the reservations made earlier in the loop, so a rejected order
        # never leaves part of the catalogue decremented.
        db.rollback()
        raise

    db.refresh(order)
    return order


def _build_order(
    member: Member, books: Dict[int, Book], data: OrderCreate, now: datetime
) -> Order:
    """Price an order against the books as they are right now; items keep the submitted order."""
    items: List[OrderItem] = [
        OrderItem(
            book_id=item.book_id,
            quantity=item.quantity,
            unit_price_cents=books[item.book_id].price_cents,
        )
        for item in data.items
    ]
    subtotal_cents = sum(item.line_total_cents for item in items)
    discount_percent = calculate_discount_percent(
        member, sum(item.quantity for item in items)
    )
    discount_cents = subtotal_cents * discount_percent // 100

    return Order(
        member_id=member.id,
        status=OrderStatus.PENDING.value,
        items=items,
        subtotal_cents=subtotal_cents,
        discount_percent=discount_percent,
        discount_cents=discount_cents,
        total_cents=subtotal_cents - discount_cents,
        created_at=now,
    )


def get_order(db: Session, order_id: int) -> Order:
    """Return an order by id, or raise 404."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def _get_pending_order(db: Session, order_id: int, action: str) -> Order:
    """Fetch an order that is still pending, or raise 404 / 409."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(
            status_code=409, detail=f"Cannot {action} an order that is {order.status}"
        )
    return order


def pay_order(db: Session, order_id: int) -> Order:
    """Mark a pending order as paid. 404 if missing; 409 if not pending."""
    order = _get_pending_order(db, order_id, "pay")
    # Stock was taken at creation time, so paying moves no copies.
    order.status = OrderStatus.PAID.value
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a pending order and restore the reserved stock. 404 if missing; 409 if not pending."""
    order = _get_pending_order(db, order_id, "cancel")
    for item in order.items:
        book_service.release_stock(db, item.book_id, item.quantity)
    order.status = OrderStatus.CANCELLED.value
    db.commit()
    db.refresh(order)
    return order
