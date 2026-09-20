"""Book catalogue operations."""
from typing import Dict,Optional

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models import Book
from app.schemas import BookCreate, BookPage, BookSort, BookUpdate, BookOut

SORT_COLUMNS : dict[str, ColumnElement] = {
    "title": Book.title.asc(),
    "-title": Book.title.desc(),
    "price": Book.price_cents.asc(),
    "-price": Book.price_cents.desc(),
}


def create_book(db: Session, data: BookCreate) -> Book:
    """Add a book to the catalogue.
    Rules: the (already normalized) ISBN must be unique -> 409 otherwise.
    """
    # TODO: reject a duplicate ISBN with 409
    book = Book(**data.model_dump())
    db.add(book)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException (
            status_code=409,
            detail="A book with this ISBN already exists"
        )
    db.refresh(book)
    return book


def get_book(db: Session, book_id: int) -> Book:
    """Return a book by id, or raise 404."""
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def update_book(db: Session, book_id: int, data: BookUpdate) -> Book:
    """Apply a partial update. Only fields present in the request are changed; 404 if missing."""
    book = get_book(db, book_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(book, field, value)
    db.commit()
    db.refresh(book)
    return book

def reserve_stock(db: Session, book_id: int, quantity: int) -> bool:
    """Reserve copies atomically.

    Returns False when the book does not exist or there is
    insufficient stock.
    The caller is responsible for committing/rolling back the
    surrounding transaction.
    """
    if(quantity <= 0):
        raise ValueError("Quantity must be greater than 0")
    
    result = db.execute(
        update(Book).where(Book.id == book_id, Book.stock >= quantity).values(stock=Book.stock - quantity)
    )
    return result.rowcount == 1 # type: ignore[attr-defined]

def release_stock(db: Session, book_id: int, quantity: int) -> None:
    """Returns stock to the catalogue."""
    if(quantity <= 0):
        raise ValueError("Quantity must be greater than 0")
    
    db.execute(
        update(Book).where(Book.id == book_id).values(stock=Book.stock + quantity)
    )

def list_books(
    db: Session,
    q: Optional[str] = None,
    restricted: Optional[bool] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    sort: Optional[BookSort] = None,
    limit: int = 20,
    offset: int = 0,
) -> BookPage:
    #"""Search the catalogue.

    # Rules:
    # - ``q`` matches title OR author, case-insensitive substring.
    # - ``restricted`` filters exactly; ``min_price``/``max_price`` are inclusive.
    # - Sorted by ``sort`` (title / price, ``-`` for descending) with ties broken by id;
    #   default order is id ascending.
    # - ``total`` counts all matches before ``limit``/``offset`` are applied.
    # """

    query = select(Book)

    if q:
        query = query.where(
            or_(
                Book.title.icontains(q, autoescape=True),
                Book.author.icontains(q, autoescape=True),
            )
        )

    if restricted is not None:
        query = query.where(Book.restricted == restricted)

    if min_price is not None:
        query = query.where(Book.price_cents >= min_price)

    if max_price is not None:
        query = query.where(Book.price_cents <= max_price)

    total = db.scalar(
        select(func.count()).select_from(query.subquery())
    ) or 0

    ordering = []

    if sort is not None:
        ordering.append(SORT_COLUMNS[sort])

    ordering.append(Book.id.asc())

    books = db.scalars(
        query
        .order_by(*ordering)
        .limit(limit)
        .offset(offset)
    ).all()

    book_items = [
        BookOut.model_validate(book)
        for book in books
    ]

    return BookPage(
        items=book_items,
        total=total,
        limit=limit,
        offset=offset,
    )
