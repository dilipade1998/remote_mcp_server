from fastmcp import FastMCP
from contextlib import contextmanager
from datetime import datetime
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "expenses.db")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

OVERALL = "overall"  # special budget "category" meaning total spend across everything
WARN_THRESHOLD = 0.8  # budget status turns to "warning" at 80% used

mcp = FastMCP("ExpenseTracker")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS budgets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,                       -- YYYY-MM
                category TEXT NOT NULL COLLATE NOCASE,     -- category name or 'overall'
                amount REAL NOT NULL,
                UNIQUE(month, category)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category)")


init_db()


def _valid_date(value, field="date"):
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{field} must be in YYYY-MM-DD format, got: {value!r}")
    return str(value)


def _valid_month(value):
    try:
        datetime.strptime(str(value), "%Y-%m")
    except ValueError:
        raise ValueError(f"month must be in YYYY-MM format, got: {value!r}")
    return str(value)


def _valid_amount(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"amount must be a number, got: {value!r}")
    if amount <= 0:
        raise ValueError("amount must be greater than 0")
    return amount


def _current_month():
    return datetime.now().strftime("%Y-%m")


def _spent(conn, month, category):
    """Total spent in a month, for one category or (if category is 'overall') everything."""
    if category.lower() == OVERALL:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM expenses WHERE substr(date, 1, 7) = ?",
            (month,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS s FROM expenses
            WHERE substr(date, 1, 7) = ? AND category = ? COLLATE NOCASE
            """,
            (month, category),
        ).fetchone()
    return row["s"]


def _budget_line(conn, month, category, budget_amount):
    spent = _spent(conn, month, category)
    used = spent / budget_amount if budget_amount else 0
    if used > 1:
        status = "exceeded"
    elif used >= WARN_THRESHOLD:
        status = "warning"
    else:
        status = "ok"
    return {
        "month": month,
        "category": category,
        "budget": budget_amount,
        "spent": spent,
        "remaining": budget_amount - spent,
        "percent_used": round(used * 100, 1),
        "status": status,
    }


def _budget_alerts(conn, date, category):
    """After a change, report any relevant budgets that are in warning/exceeded state."""
    month = date[:7]
    alerts = []
    for cat in (category, OVERALL):
        row = conn.execute(
            "SELECT category, amount FROM budgets WHERE month = ? AND category = ? COLLATE NOCASE",
            (month, cat),
        ).fetchone()
        if row:
            line = _budget_line(conn, month, row["category"], row["amount"])
            if line["status"] != "ok":
                alerts.append(line)
    return alerts


# ----------------------------------------------------------------------------
# Expense tools
# ----------------------------------------------------------------------------
@mcp.tool()
def add_expense(date, amount, category, subcategory="", note=""):
    '''Add a new expense entry to the database. Date format: YYYY-MM-DD.
    Returns any budget warnings triggered by this expense.'''
    date = _valid_date(date)
    amount = _valid_amount(amount)
    with db() as c:
        cur = c.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note) VALUES (?,?,?,?,?)",
            (date, amount, category, subcategory, note),
        )
        result = {"status": "ok", "id": cur.lastrowid}
        alerts = _budget_alerts(c, date, category)
        if alerts:
            result["budget_alerts"] = alerts
        return result


@mcp.tool()
def get_expense(expense_id):
    '''Get a single expense by its id.'''
    with db() as c:
        row = c.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if not row:
            return {"status": "error", "message": f"No expense with id {expense_id}"}
        return dict(row)


@mcp.tool()
def list_expenses(start_date, end_date):
    '''List expense entries within an inclusive date range.'''
    _valid_date(start_date, "start_date")
    _valid_date(end_date, "end_date")
    with db() as c:
        rows = c.execute(
            """
            SELECT id, date, amount, category, subcategory, note
            FROM expenses
            WHERE date BETWEEN ? AND ?
            ORDER BY date ASC, id ASC
            """,
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


@mcp.tool()
def edit_expense(expense_id, date=None, amount=None, category=None, subcategory=None, note=None):
    '''Edit an existing expense. Only the fields you pass are changed;
    everything else stays as it was.'''
    updates = {}
    if date is not None:
        updates["date"] = _valid_date(date)
    if amount is not None:
        updates["amount"] = _valid_amount(amount)
    if category is not None:
        updates["category"] = category
    if subcategory is not None:
        updates["subcategory"] = subcategory
    if note is not None:
        updates["note"] = note

    if not updates:
        return {"status": "error", "message": "No fields provided to update"}

    with db() as c:
        existing = c.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if not existing:
            return {"status": "error", "message": f"No expense with id {expense_id}"}

        set_clause = ", ".join(f"{col} = ?" for col in updates)
        c.execute(
            f"UPDATE expenses SET {set_clause} WHERE id = ?",
            (*updates.values(), expense_id),
        )
        updated = dict(c.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone())

        result = {"status": "ok", "before": dict(existing), "after": updated}
        alerts = _budget_alerts(c, updated["date"], updated["category"])
        if alerts:
            result["budget_alerts"] = alerts
        return result


@mcp.tool()
def delete_expense(expense_id):
    '''Delete an expense by id. Returns the deleted entry so it can be re-added if needed.'''
    with db() as c:
        row = c.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if not row:
            return {"status": "error", "message": f"No expense with id {expense_id}"}
        c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        return {"status": "ok", "deleted": dict(row)}


@mcp.tool()
def search_expenses(query=None, category=None, min_amount=None, max_amount=None,
                    start_date=None, end_date=None, limit=50):
    '''Search expenses. All filters are optional and combined with AND.
    `query` matches text in the note or subcategory.'''
    sql = "SELECT id, date, amount, category, subcategory, note FROM expenses WHERE 1=1"
    params = []

    if query:
        sql += " AND (note LIKE ? OR subcategory LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    if category:
        sql += " AND category = ? COLLATE NOCASE"
        params.append(category)
    if min_amount is not None:
        sql += " AND amount >= ?"
        params.append(float(min_amount))
    if max_amount is not None:
        sql += " AND amount <= ?"
        params.append(float(max_amount))
    if start_date:
        sql += " AND date >= ?"
        params.append(_valid_date(start_date, "start_date"))
    if end_date:
        sql += " AND date <= ?"
        params.append(_valid_date(end_date, "end_date"))

    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    params.append(int(limit))

    with db() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


# ----------------------------------------------------------------------------
# Summaries
# ----------------------------------------------------------------------------
@mcp.tool()
def summarize(start_date, end_date, category=None):
    '''Summarize expenses by category within an inclusive date range.
    Includes the number of entries per category and a grand total.'''
    _valid_date(start_date, "start_date")
    _valid_date(end_date, "end_date")

    sql = """
        SELECT category, SUM(amount) AS total_amount, COUNT(*) AS count
        FROM expenses
        WHERE date BETWEEN ? AND ?
    """
    params = [start_date, end_date]
    if category:
        sql += " AND category = ? COLLATE NOCASE"
        params.append(category)
    sql += " GROUP BY category ORDER BY total_amount DESC"

    with db() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    return {
        "start_date": start_date,
        "end_date": end_date,
        "by_category": rows,
        "grand_total": sum(r["total_amount"] for r in rows),
        "expense_count": sum(r["count"] for r in rows),
    }


@mcp.tool()
def monthly_report(month=None):
    '''Full report for a month (YYYY-MM, defaults to the current month):
    total spent, spend by category, biggest expenses, daily average,
    comparison with the previous month, and budget status.'''
    month = _valid_month(month) if month else _current_month()

    year, mon = map(int, month.split("-"))
    prev_month = f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"

    with db() as c:
        by_category = [
            dict(r) for r in c.execute(
                """
                SELECT category, SUM(amount) AS total_amount, COUNT(*) AS count
                FROM expenses WHERE substr(date, 1, 7) = ?
                GROUP BY category ORDER BY total_amount DESC
                """,
                (month,),
            ).fetchall()
        ]
        total = sum(r["total_amount"] for r in by_category)
        top_expenses = [
            dict(r) for r in c.execute(
                """
                SELECT id, date, amount, category, note FROM expenses
                WHERE substr(date, 1, 7) = ? ORDER BY amount DESC LIMIT 5
                """,
                (month,),
            ).fetchall()
        ]
        days_with_spend = c.execute(
            "SELECT COUNT(DISTINCT date) AS d FROM expenses WHERE substr(date, 1, 7) = ?",
            (month,),
        ).fetchone()["d"]
        prev_total = _spent(c, prev_month, OVERALL)
        budgets = [
            _budget_line(c, month, r["category"], r["amount"])
            for r in c.execute(
                "SELECT category, amount FROM budgets WHERE month = ? ORDER BY category", (month,)
            ).fetchall()
        ]

    change_pct = round((total - prev_total) / prev_total * 100, 1) if prev_total else None
    return {
        "month": month,
        "total_spent": total,
        "by_category": by_category,
        "top_expenses": top_expenses,
        "avg_per_active_day": round(total / days_with_spend, 2) if days_with_spend else 0,
        "previous_month": {"month": prev_month, "total_spent": prev_total, "change_percent": change_pct},
        "budgets": budgets,
    }


# ----------------------------------------------------------------------------
# Budget tools
# ----------------------------------------------------------------------------
@mcp.tool()
def set_budget(amount, category="overall", month=None):
    '''Set (or update) a monthly budget. `category` can be a category name or
    "overall" for a total monthly budget. `month` is YYYY-MM (defaults to the current month).'''
    amount = _valid_amount(amount)
    month = _valid_month(month) if month else _current_month()
    with db() as c:
        c.execute(
            """
            INSERT INTO budgets(month, category, amount) VALUES (?,?,?)
            ON CONFLICT(month, category) DO UPDATE SET amount = excluded.amount
            """,
            (month, category, amount),
        )
        return {"status": "ok", **_budget_line(c, month, category, amount)}


@mcp.tool()
def list_budgets(month=None):
    '''List all budgets for a month (YYYY-MM, defaults to the current month).'''
    month = _valid_month(month) if month else _current_month()
    with db() as c:
        rows = c.execute(
            "SELECT category, amount FROM budgets WHERE month = ? ORDER BY category", (month,)
        ).fetchall()
        return [
            {"id": None, **_budget_line(c, month, r["category"], r["amount"])} for r in rows
        ]


@mcp.tool()
def budget_status(month=None):
    '''Show spent vs budget for every budget in a month, with warnings for any
    that are close to (>=80%) or over their limit.'''
    month = _valid_month(month) if month else _current_month()
    with db() as c:
        rows = c.execute(
            "SELECT category, amount FROM budgets WHERE month = ? ORDER BY category", (month,)
        ).fetchall()
        lines = [_budget_line(c, month, r["category"], r["amount"]) for r in rows]
    return {
        "month": month,
        "budgets": lines,
        "needs_attention": [l for l in lines if l["status"] != "ok"],
    }


@mcp.tool()
def delete_budget(category="overall", month=None):
    '''Delete a monthly budget for a category (or "overall").'''
    month = _valid_month(month) if month else _current_month()
    with db() as c:
        cur = c.execute(
            "DELETE FROM budgets WHERE month = ? AND category = ? COLLATE NOCASE",
            (month, category),
        )
        if cur.rowcount == 0:
            return {"status": "error", "message": f"No budget for '{category}' in {month}"}
        return {"status": "ok", "deleted": {"month": month, "category": category}}


# ----------------------------------------------------------------------------
# Resources
# ----------------------------------------------------------------------------
@mcp.resource("expense://categories", mime_type="application/json")
def categories():
    # Read fresh each time so you can edit the file without restarting
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    mcp.run()