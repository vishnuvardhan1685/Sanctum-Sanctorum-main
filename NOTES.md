> **Live URL:** https://sanctum-sanctorum-main.vercel.app

> **Database:** Neon PostgreSQL  
> **Deployment:** Vercel

The application has been completed according to `ASSIGNMENT.md` and `SPEC.md`. I also added three tests for concurrent stock reservation as an optional extra, since stock consistency is an important part of the bookstore domain.

---

## 1. Where the project started vs. where it ended up

The starter repository was a partially implemented FastAPI application. A number of the routers and schemas were already present, but many of the actual business operations were either incomplete, marked with `TODO`s, or implemented only partially.

I worked through the implementation in the order suggested by `ASSIGNMENT.md`:

1. Books
2. Members
3. Orders
4. Loans
5. Member statistics and reports
6. Concurrency handling
7. Frontend integration
8. Deployment

The original tests were not modified. I added only three new tests for the optional concurrent-stock requirement.

### Test status

| | Original scaffold | Final submission |
|---|---:|---:|
| Passed | 73 | **205** |
| Failed | 125 | **0** |
| Errors | 4 | **0** |
| Additional tests | 0 | **3 concurrency tests** |

The final test suite passes locally using the required SQLite setup without depending on the deployed database.

---

# 2. What was finished

## Books

### ISBN-13 validation

The original ISBN normalization removed spaces/hyphens and checked that the ISBN contained 13 digits, but the actual ISBN-13 check digit validation was still missing.

I implemented the ISBN-13 checksum according to the specification:

- alternating weights of `1` and `3`,
- calculating the checksum from the first 12 digits,
- validating the 13th digit.

This validation is performed through the existing ISBN normalization/validation flow.

### Duplicate ISBN → 409

Book creation originally inserted the book directly and allowed a database `IntegrityError` to escape.

I changed this so that the database uniqueness constraint remains the source of truth:

1. Attempt the insert.
2. Commit the transaction.
3. If the database raises `IntegrityError`, roll back.
4. Return HTTP `409`.

I deliberately did not perform a separate "does this ISBN already exist?" query before inserting. Apart from being unnecessary, a pre-check introduces a race condition between checking and inserting.

### Updating books

The `PATCH /books/{book_id}` endpoint was incomplete.

I implemented:

- partial updates,
- `exclude_unset=True` so omitted fields are left unchanged,
- validation through the existing Pydantic schema,
- protection against changing the ISBN.

The ISBN is not part of the patchable fields, matching the specification.

### Book listing

The original catalogue implementation was missing several required behaviors.

I implemented:

- title search,
- author search,
- minimum price filtering,
- maximum price filtering,
- sorting,
- pagination,
- correct total-count calculation.

Search now matches either title or author.

Sorting uses an explicit mapping from the allowed sort values to SQLAlchemy columns, and `Book.id` is also included as a deterministic tie-breaker.

The total count is calculated **before** applying pagination, so `total` represents the complete filtered catalogue rather than just the current page.

---

# 3. Members

## Membership tier comparison

There was an off-by-one bug in the existing `tier_at_least` helper.

The original comparison used:

```python
TIER_ORDER.index(tier) > TIER_ORDER.index(minimum)
```

This incorrectly rejected a member whose tier was exactly equal to the required minimum.

For example, a `master` member should satisfy a requirement of `master or above`, but `>` would return false.

I changed the comparison to `>=`.

This is important for restricted books and loans because the specification explicitly states that Master members and above can access restricted content.

## Email normalization

Member emails are normalized before they reach the service layer:

- surrounding whitespace is removed,
- the email is converted to lowercase.

This means logically identical emails such as:

```text
User@example.com
 user@example.com
```

are treated consistently.

## Duplicate email → 409

Member creation uses the database uniqueness constraint and converts a duplicate `IntegrityError` into HTTP `409`.

As with ISBNs, I intentionally did not perform a pre-insert existence check because the database constraint is the reliable source of truth when concurrent requests are possible.

## Member statistics

`get_member_stats` was originally unimplemented.

I implemented statistics for:

- paid orders,
- total amount spent,
- loan-related information,
- overdue loans.

For paid orders, the counts and sums are calculated using a SQL aggregate query.

For loans, I use the application's existing `Loan.is_overdue(now)` logic rather than recreating the overdue condition separately in SQL. This keeps the definition of "overdue" in one place.

## GET /members

I also implemented the optional `GET /members` endpoint with pagination.

It follows the same general pagination approach as the books endpoint:

- `limit`,
- `offset`,
- total count.

---

# 4. Orders

Orders were one of the largest incomplete areas of the starter repository.

## Order validation

The specification requires validation errors to happen before later business checks.

I therefore moved the following validation into the Pydantic schema:

- order must contain at least one item,
- duplicate `book_id`s are not allowed.

This means these invalid requests fail with `422` before database lookups or other business rules are evaluated.

## Order creation

The order flow follows the validation order specified by the assignment:

1. Validate the request schema.
2. Find the member.
3. Resolve all referenced books.
4. Check restricted-book access.
5. Reserve stock.
6. Build the order and calculate its price.
7. Commit the transaction.

I deliberately resolve all requested books before performing the later access and stock checks.

This matters when an order contains multiple problematic items. For example, if one book does not exist and another book is restricted, the missing book should produce the appropriate `404` rather than the restricted book producing a `403`.

## All-or-nothing stock reservation

Stock changes are treated as one transactional operation.

If an order contains multiple books and stock reservation succeeds for the first item but fails for a later item:

```text
Book A → stock reserved
Book B → insufficient stock
```

the transaction is rolled back.

Therefore:

```text
Book A → reservation also disappears
Book B → unchanged
Order → not created
```

This prevents a failed order from leaving the database in a partially modified state.

## Pricing

Order prices are calculated using the book's current price at the time the order is created.

The order stores the resulting `unit_price_cents`, meaning later changes to the catalogue price do not change existing orders.

The final price calculation includes:

- subtotal,
- member-tier discount,
- additional quantity discount when the total quantity reaches the specified threshold,
- discount amount,
- final total.

Discount amounts are calculated using integer cents as required by the specification.

## Cancelling orders

The original cancellation logic changed the order status but did not restore the reserved stock.

I changed cancellation so that the stock reserved by the order is released when the order is cancelled.

Payment does not reserve stock again because stock was already reserved when the order was created.

---

# 5. Loans

The specification explicitly stated that the Loan model was incomplete, so this required both model and service-layer work.

## Loan model

I added:

- `due_at`,
- `returned_at`,
- `late_fee_cents`.

I also added:

```python
Loan.is_overdue(now)
```

so that the definition of an overdue loan is centralized.

A loan is overdue only when:

```text
returned_at is None
AND
now > due_at
```

Therefore, if the current time is exactly equal to `due_at`, the loan is still considered active.

## Loan status

Loan status is calculated when the loan is read rather than stored as a database field.

This avoids stale status values.

For example, a loan does not need a background process to change from:

```text
active
```

to:

```text
overdue
```

when its due date passes.

The next read simply calculates the current status using `is_overdue(now)`.

## Late fees

Late fees are calculated using the specification's rule:

- calculate how long the book is overdue,
- any started/partial day counts as a full day,
- calculate the corresponding late fee,
- cap the late fee at the current price of the book.

The book price used for the cap is the price at **return time**, as required by the specification.

## Borrowing rules

Loan creation checks the rules in the required order:

1. Member exists → `404`
2. Book exists → `404`
3. Restricted book requires sufficient membership tier → `403`
4. Member has no overdue loan → `409`
5. Member does not already hold the same book → `409`
6. Member has not reached their tier loan limit → `409`
7. Book has sufficient stock → `409`

The member's open loans are loaded once and reused for the relevant checks rather than querying the database repeatedly.

## Returning books

Returning a loan:

1. verifies that it has not already been returned,
2. sets `returned_at`,
3. calculates the late fee,
4. stores the late fee,
5. releases the book stock,
6. commits the transaction.

A second attempt to return the same loan results in `409` rather than releasing the same stock twice.

---

# 6. Member statistics and reports

## Member statistics

Member statistics use paid orders for purchase-related statistics and the loan state for loan-related statistics.

For overdue loans, the same `Loan.is_overdue(now)` logic used by the loan endpoints is reused here.

This avoids having different definitions of "overdue" in different parts of the application.

## Top books report

The `top_books` report was originally a stub.

I implemented it using a query joining:

```text
Book
  ↓
OrderItem
  ↓
Order
```

and filtering orders to:

```text
status = paid
```

The results are grouped by book and ordered by:

1. copies sold descending,
2. title ascending.

Books with no paid sales naturally do not appear because the query uses the relevant inner joins.

---

# 7. Concurrent stock reservation

I also implemented the optional concurrency requirement from the assignment:

> Handle concurrent orders for the last copy of a book safely.

This was an important business-level improvement because a normal implementation like:

```text
read stock
↓
if stock > 0
↓
decrement stock
```

contains a race condition.

Two requests can both read:

```text
stock = 1
```

and both believe they are allowed to reserve the final copy.

## Atomic stock reservation

I changed stock reservation to use a single SQL update:

```python
db.execute(
    update(Book)
    .where(Book.id == book_id, Book.stock >= quantity)
    .values(stock=Book.stock - quantity)
)
```

The operation succeeds only if the database actually updates the row.

Conceptually:

```text
Request A ─┐
           ├── UPDATE ... WHERE stock >= 1
Request B ─┘
```

Only one request can successfully reserve the final available copy.

The second request sees that the condition is no longer satisfied and therefore fails the reservation.

This makes the stock reservation operation atomic instead of relying on a separate read/check/write sequence.

## Tests added

I added **three concurrency-related test cases** in:

```text
tests/test_stock_concurrency.py
```

These tests exercise the last-copy race using separate database sessions rather than relying on an HTTP `TestClient` request sequence.

The important point is that a sequential HTTP test would not actually reproduce the race condition. Testing the reservation operation with separate sessions allows the database-level behavior to be exercised directly.

The implementation works with both the local SQLite test setup and the PostgreSQL deployment.

---

# 8. Frontend changes

Although the main assignment focuses on the backend, I also fixed the supplied frontend behavior so that the UI correctly reflects the implemented business rules.

## Restricted books

The restricted-book rule was already enforced by the backend.

I also added corresponding client-side behavior:

- restricted books cannot be added to the cart by members below Master,
- the Add to Cart button is disabled for those members,
- switching to a lower-tier member removes restricted books that may already be in the cart.

The backend remains the final authority for enforcing the rule.

## Refreshing application state

Several actions were previously updating the backend without immediately refreshing all dependent frontend state.

I updated the relevant action handlers to use `async/await` correctly and refresh the appropriate data after mutations.

This includes:

- checkout,
- payment,
- order cancellation,
- borrowing,
- returning,
- catalogue refresh,
- member statistics refresh,
- loan refresh.

As a result, changes to:

- stock,
- member statistics,
- loan status,

are reflected immediately instead of waiting for another unrelated UI refresh.

---

# 9. Database and deployment

The application originally used SQLite for local development and testing.

For deployment, I moved the application to:

```text
Vercel
    ↓
FastAPI
    ↓
Neon PostgreSQL
```

I retained SQLite as the default local database so that:

```bash
uv run pytest
```

continues to work without requiring an external database.

## Deployment

The final application is deployed using:

- **Vercel** for the application,
- **Neon** for PostgreSQL.

The assignment suggested Supabase + Vercel, but explicitly allowed equivalent services and also listed Neon as an acceptable hosted PostgreSQL provider.

I therefore kept Neon rather than introducing an unnecessary database migration after the PostgreSQL setup had already been implemented and tested.

---

# 10. Spec decisions and judgment calls

I treated `SPEC.md` as the source of truth whenever general business intuition could have suggested a different behavior.

### Membership boundary

The `master or above` requirement means that a Master member must be allowed to access restricted content.

The original `tier_at_least` helper used `>` instead of `>=`, which contradicted the specification. I corrected it.

### Order validation ordering

The specification requires schema validation to happen before later `404`, `403`, and `409` checks.

Putting empty-order and duplicate-book validation into the Pydantic schema naturally ensures that FastAPI rejects those requests before the service layer performs database lookups.

### Multiple invalid books in an order

All referenced books are resolved before restricted-access and stock checks.

This ensures that a missing book is reported correctly even if another book in the same request is restricted.

### Loan status

I chose to calculate loan status at read time rather than store it.

This prevents a stored `active/overdue` value from becoming stale when time passes.

### Overdue boundary

A loan due exactly at the current time is still active.

Only:

```text
now > due_at
```

makes the loan overdue.

### Mixed-case sorting ties

The specification leaves mixed-case sort tie behavior unspecified, so I did not add unnecessary custom collation behavior.

---

# 11. AI usage

AI tools were deliberately used as development assistants throughout the assignment.

The main goal was not to blindly generate the implementation, but to use AI to understand the existing codebase, structure the work, review decisions, and move through the assignment efficiently while still validating the final implementation against the specification and tests.

## How I used AI

I first gave the complete repository to **Claude** and asked it to analyze the project and extract the clear rules that needed to be followed during implementation.

From that analysis, Claude and I structured the implementation plan.

We then broke the work into a logical order so that dependencies between features were handled correctly rather than implementing everything independently.

The implementation order was broadly:

```text
Books
  ↓
Members
  ↓
Orders
  ↓
Loans
  ↓
Statistics / Reports
  ↓
Concurrency
  ↓
Frontend integration
  ↓
Deployment
```

After establishing the plan, I used Claude to identify and propose the specific code changes required for each stage.

I then used **ChatGPT** as a second layer of review, especially for checking whether the proposed logic made sense from a real business/data-integrity perspective and whether a proposed change was actually justified by the specification.

This was useful because I did not want to accept an implementation simply because it passed one test. I wanted to understand whether the design made sense for the bookstore domain.

## Example: concurrency / race condition

One of the outcomes of this review process was identifying the importance of handling concurrent stock reservations safely.

A simple implementation could do:

```text
read stock
↓
check stock > 0
↓
decrement stock
```

but that has a check-then-act race condition.

After reviewing this from the business perspective, I implemented atomic stock reservation at the database level and added **three tests** specifically for the concurrency behavior.

These tests helped verify that two concurrent operations cannot both successfully reserve the same final copy of a book.

## AI was used as a reviewer, not as the source of truth

Throughout the implementation I treated:

1. `SPEC.md`
2. `ASSIGNMENT.md`
3. the existing project structure
4. the test suite
5. actual application behavior

as the final sources of truth.

When an AI suggestion did not fit the specification or introduced an unnecessary business rule, I did not apply it simply because it was suggested.

This was particularly important when discussing what would be "normal" business behavior versus what this specific assignment actually required.

The final implementation was reviewed and tested by me, and I understand the reasoning behind the changes made.

---

# 12. Final summary

The main objective of the implementation was not just to make the test suite pass, but to keep the application consistent with the specification and maintain a clean separation between the API layer, business logic, and database layer.

The main business logic is kept in services, routers remain relatively thin, database constraints are used for data integrity, and operations that modify multiple pieces of state are handled transactionally.

The optional concurrency requirement was also implemented using an atomic database-level stock reservation rather than a simple read/check/write approach.

The final application is deployed on Vercel with Neon PostgreSQL, while the local test setup continues to work with SQLite and does not depend on an external service.

The implementation was developed with AI assistance, but the specification, tests, and actual application behavior were used as the final source of truth throughout the process.