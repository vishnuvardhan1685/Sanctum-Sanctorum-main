"""Stock reservation must be safe when two sessions race for the same copy.

This is the one rule the HTTP-level suite cannot express: TestClient calls are sequential, so
a check-then-act implementation passes every test in ``test_orders.py`` while still overselling
under real concurrency.  These tests drive the service directly with two sessions instead.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Book
from app.services.books import release_stock, reserve_stock


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as first, factory() as second:
        yield first, second
    engine.dispose()


@pytest.fixture
def last_copy(sessions):
    first, _ = sessions
    book = Book(title="Darkhold", author="Unknown", isbn="9780679724773", price_cents=9999, stock=1)
    first.add(book)
    first.commit()
    return book.id


def test_only_one_session_can_take_the_last_copy(sessions, last_copy):
    first, second = sessions

    # Both sessions have seen stock == 1 before either of them writes.
    assert first.get(Book, last_copy).stock == 1
    assert second.get(Book, last_copy).stock == 1

    assert reserve_stock(first, last_copy, 1) is True
    first.commit()

    assert reserve_stock(second, last_copy, 1) is False
    second.rollback()

    assert first.get(Book, last_copy).stock == 0


def test_reserving_more_than_available_changes_nothing(sessions, last_copy):
    first, _ = sessions

    assert reserve_stock(first, last_copy, 2) is False
    first.commit()

    assert first.get(Book, last_copy).stock == 1


def test_release_puts_the_copy_back(sessions, last_copy):
    first, _ = sessions

    reserve_stock(first, last_copy, 1)
    release_stock(first, last_copy, 1)
    first.commit()

    assert first.get(Book, last_copy).stock == 1