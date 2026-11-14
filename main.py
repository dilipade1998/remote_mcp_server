"""
Expense Tracker MCP server (FastMCP + aiosqlite)

Tools
-----
Expenses : add_expense, get_expense, edit_expense, delete_expense,
           list_expenses, summarize
Budgets  : set_budget, list_budgets, delete_budget, budget_status
Other    : list_categories

Budgets can be daily / weekly / monthly / yearly, per category or for
everything combined (category = "All"). Warnings are returned whenever an
add / edit pushes a budget to >= 80% (warning) or >= 100% (exceeded).

Env vars (all optional)
-----------------------
EXPENSE_DB_PATH  full path of the sqlite file
CURRENCY         symbol used in messages (default: ₹)
BUDGET_WARN_PCT  warning threshold in percent (default: 80)
PORT             HTTP port (default: 8000)
"""
import calendar
import datetime as dt
import functools
import json
import math
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

import aiosqlite
from fastmcp import FastMCP

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
CURRENCY = os.environ.get("CURRENCY", "₹")
WARN_PCT = float(os.environ.get("BUDGET_WARN_PCT", "80"))
CATEGORIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "categories.json")
ALL = "All"  # budget key that covers every category
PERIODS = ("daily", "weekly", "monthly", "yearly")


def _resolve_db_path() -> str:
    """Prefer a persistent location; fall back to the temp dir if not writable."""
    explicit = os.environ.get("EXPENSE_DB_PATH")
    candidates = [explicit] if explicit else [
        os.path.join(os.path.expanduser("~"), ".expense_tracker", "expenses.db"),
        os.path.join(tempfile.gettempdir(), "expenses.db"),
    ]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with sqlite3.connect(path) as c:
                c.execute("CREATE TABLE IF NOT EXISTS _probe(x)")
                c.execute("DROP TABLE _probe")
            return path
        except Exception:
            continue
    raise RuntimeError("No writable location found for the expenses database")


DB_PATH = _resolve_db_path()
print(f"Database path: {DB_PATH}")

mcp = FastMCP("ExpenseTracker")

# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
DEFAULT_CATEGORIES = [
    "Food & Dining", "Transportation", "Shopping", "Entertainment",
    "Bills & Utilities", "Healthcare", "Travel", "Education", "Business", "Other",
]
ALIASES = {
    "food": "Food & Dining", "meal": "Food & Dining", "meals": "Food & Dining",
    "dining": "Food & Dining", "groceries": "Food & Dining", "grocery": "Food & Dining",
    "transport": "Transportation", "fuel": "Transportation", "taxi": "Transportation",
    "bills": "Bills & Utilities", "utilities": "Bills & Utilities",
    "health": "Healthcare", "medical": "Healthcare",
    "fun": "Entertainment", "movies": "Entertainment",
}


def load_categories() -> list:
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cats = data.get("categories") if isinstance(data, dict) else data
        if cats:
            return [str(c) for c in cats]
    except Exception:
        pass
    return DEFAULT_CATEGORIES


def normalize_category(raw) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ValueError("category is required")
    key = name.lower()
    for cat in load_categories():
        if cat.lower() == key:
            return cat
    if key in ALIASES:
        return ALIASES[key]
    return name.title()  # unknown -> accepted as a custom category


def normalize_budget_category(raw) -> str:
    if str(raw or "").strip().lower() in ("all", "total", "overall"):
        return ALL
    return normalize_category(raw)


# --------------------------------------------------------------------------
# Validation + helpers
# --------------------------------------------------------------------------
def parse_date(value, default_today: bool = False) -> str:
    if value in (None, ""):
        if default_today:
            return dt.date.today().isoformat()
        raise ValueError("date is required (format YYYY-MM-DD)")
    try:
        return dt.datetime.strptime(str(value).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD.")


def parse_amount(value) -> float:
    try:
        amt = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid amount '{value}'. Must be a number.")
    if not math.isfinite(amt) or amt <= 0:
        raise ValueError("amount must be greater than zero")
    return amt


def money(x: float) -> str:
    return f"{CURRENCY}{x:,.2f}"


def period_window(period: str, ref: dt.date):
    """Start/end dates of the budget period that contains `ref`."""
    if period == "daily":
        return ref, ref
    if period == "weekly":
        start = ref - dt.timedelta(days=ref.weekday())  # Monday
        return start, start + dt.timedelta(days=6)
    if period == "monthly":
        last = calendar.monthrange(ref.year, ref.month)[1]
        return ref.replace(day=1), ref.replace(day=last)
    return dt.date(ref.year, 1, 1), dt.date(ref.year, 12, 31)


def safe(fn):
    """Turn any error into a clean {"status": "error"} response."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except aiosqlite.Error as e:
            if "readonly" in str(e).lower():
                return {"status": "error", "message": "Database is read-only. Check file permissions."}
            return {"status": "error", "message": f"Database error: {e}"}
        except Exception as e:
            return {"status": "error", "message": f"Unexpected error: {e}"}
    return wrapper


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("PRAGMA journal_mode=WAL")
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
                category TEXT PRIMARY KEY COLLATE NOCASE,
                amount REAL NOT NULL,
                period TEXT NOT NULL DEFAULT 'monthly'
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category)")
    print("Database initialized")


init_db()


@asynccontextmanager
async def db():
    async with aiosqlite.connect(DB_PATH) as c:
        c.row_factory = aiosqlite.Row
        await c.execute("PRAGMA busy_timeout=5000")
        yield c


async def fetch_expense(c, expense_id) -> Optional[dict]:
    cur = await c.execute(
        "SELECT id, date, amount, category, subcategory, note FROM expenses WHERE id = ?",
        (expense_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Budget logic
# --------------------------------------------------------------------------
async def spent_in_window(c, category: str, start: dt.date, end: dt.date) -> float:
    sql = "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN ? AND ?"
    params = [start.isoformat(), end.isoformat()]
    if category != ALL:
        sql += " AND category = ? COLLATE NOCASE"
        params.append(category)
    cur = await c.execute(sql, params)
    row = await cur.fetchone()
    return round(row[0], 2)


async def budget_status_for(c, budget: dict, ref: dt.date) -> dict:
    start, end = period_window(budget["period"], ref)
    spent = await spent_in_window(c, budget["category"], start, end)
    limit = budget["amount"]
    pct = round(spent / limit * 100, 1) if limit else 0.0
    if pct >= 100:
        state = "exceeded"
    elif pct >= WARN_PCT:
        state = "warning"
    else:
        state = "ok"
    return {
        "category": budget["category"],
        "period": budget["period"],
        "window": f"{start.isoformat()} to {end.isoformat()}",
        "budget": limit,
        "spent": spent,
        "remaining": round(limit - spent, 2),
        "percent_used": pct,
        "state": state,
    }


async def budget_warnings(c, category: str, date_str: str) -> list:
    """Warnings for budgets affected by an expense in `category` on `date_str`."""
    ref = dt.date.fromisoformat(date_str)
    cur = await c.execute(
        "SELECT category, amount, period FROM budgets WHERE category = ? COLLATE NOCASE OR category = ?",
        (category, ALL),
    )
    warnings = []
    for row in await cur.fetchall():
        s = await budget_status_for(c, dict(row), ref)
        if s["state"] == "ok":
            continue
        label = "all expenses" if s["category"] == ALL else s["category"]
        if s["state"] == "exceeded":
            msg = (f"Budget EXCEEDED for {label}: spent {money(s['spent'])} of "
                   f"{money(s['budget'])} {s['period']} ({s['percent_used']}%)")
        else:
            msg = (f"Budget warning for {label}: spent {money(s['spent'])} of "
                   f"{money(s['budget'])} {s['period']} ({s['percent_used']}%), "
                   f"{money(s['remaining'])} left")
        warnings.append({"state": s["state"], "message": msg, **s})
    return warnings


# --------------------------------------------------------------------------
# Expense tools
# --------------------------------------------------------------------------
@mcp.tool()
@safe
async def add_expense(
    amount: float,
    category: str,
    date: Optional[str] = None,
    subcategory: str = "",
    note: str = "",
) -> dict:
    """Add a new expense. Date format YYYY-MM-DD (defaults to today).
    Returns the new id plus any budget warnings triggered by this expense."""
    d = parse_date(date, default_today=True)
    amt = parse_amount(amount)
    cat = normalize_category(category)
    async with db() as c:
        cur = await c.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note) VALUES (?,?,?,?,?)",
            (d, amt, cat, subcategory.strip(), note.strip()),
        )
        await c.commit()
        new_id = cur.lastrowid
        expense = await fetch_expense(c, new_id)
        warnings = await budget_warnings(c, cat, d)
    resp = {"status": "success", "id": new_id, "expense": expense,
            "message": "Expense added successfully"}
    if warnings:
        resp["budget_warnings"] = warnings
    return resp


@mcp.tool()
@safe
async def get_expense(expense_id: int) -> dict:
    """Get a single expense by its id."""
    async with db() as c:
        expense = await fetch_expense(c, expense_id)
    if not expense:
        return {"status": "error", "message": f"No expense with id {expense_id}"}
    return {"status": "success", "expense": expense}


@mcp.tool()
@safe
async def edit_expense(
    expense_id: int,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    date: Optional[str] = None,
    subcategory: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Edit an existing expense. Only the fields you pass are changed."""
    updates = {}
    if amount is not None:
        updates["amount"] = parse_amount(amount)
    if category is not None:
        updates["category"] = normalize_category(category)
    if date is not None:
        updates["date"] = parse_date(date)
    if subcategory is not None:
        updates["subcategory"] = subcategory.strip()
    if note is not None:
        updates["note"] = note.strip()
    if not updates:
        raise ValueError("Nothing to update. Pass at least one field to change.")

    async with db() as c:
        before = await fetch_expense(c, expense_id)
        if not before:
            return {"status": "error", "message": f"No expense with id {expense_id}"}
        set_clause = ", ".join(f"{k} = ?" for k in updates)  # keys are whitelisted above
        await c.execute(f"UPDATE expenses SET {set_clause} WHERE id = ?",
                        (*updates.values(), expense_id))
        await c.commit()
        after = await fetch_expense(c, expense_id)
        warnings = await budget_warnings(c, after["category"], after["date"])
    resp = {"status": "success", "before": before, "after": after,
            "message": "Expense updated successfully"}
    if warnings:
        resp["budget_warnings"] = warnings
    return resp


@mcp.tool()
@safe
async def delete_expense(expense_id: int) -> dict:
    """Delete an expense by id. Returns the deleted entry so it can be re-added if needed."""
    async with db() as c:
        existing = await fetch_expense(c, expense_id)
        if not existing:
            return {"status": "error", "message": f"No expense with id {expense_id}"}
        await c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        await c.commit()
    return {"status": "success", "deleted": existing, "message": "Expense deleted successfully"}


@mcp.tool()
@safe
async def list_expenses(
    start_date: str,
    end_date: str,
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """List expenses in an inclusive date range (YYYY-MM-DD), newest first.
    Optionally filter by category and/or a keyword found in note/subcategory."""
    s, e = parse_date(start_date), parse_date(end_date)
    if s > e:
        raise ValueError("start_date must not be after end_date")
    limit = max(1, min(int(limit), 1000))

    where = ["date BETWEEN ? AND ?"]
    params = [s, e]
    if category:
        where.append("category = ? COLLATE NOCASE")
        params.append(normalize_category(category))
    if keyword:
        where.append("(note LIKE ? OR subcategory LIKE ?)")
        params += [f"%{keyword}%", f"%{keyword}%"]
    where_sql = " AND ".join(where)

    async with db() as c:
        cur = await c.execute(
            f"SELECT id, date, amount, category, subcategory, note FROM expenses "
            f"WHERE {where_sql} ORDER BY date DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        cur = await c.execute(
            f"SELECT COALESCE(SUM(amount),0), COUNT(*) FROM expenses WHERE {where_sql}", params
        )
        total, count = await cur.fetchone()
    return {"status": "success", "count": count, "total": round(total, 2),
            "returned": len(rows), "expenses": rows}


@mcp.tool()
@safe
async def summarize(
    start_date: str,
    end_date: str,
    category: Optional[str] = None,
    group_by: str = "category",
) -> dict:
    """Summarize expenses in an inclusive date range.
    group_by: category | subcategory | day | month. Optional category filter."""
    s, e = parse_date(start_date), parse_date(end_date)
    if s > e:
        raise ValueError("start_date must not be after end_date")
    group_exprs = {
        "category": "category COLLATE NOCASE",
        "subcategory": "category COLLATE NOCASE, subcategory",
        "day": "date",
        "month": "substr(date, 1, 7)",
    }
    if group_by not in group_exprs:
        raise ValueError(f"group_by must be one of: {', '.join(group_exprs)}")
    select_expr = {
        "category": "category AS grp",
        "subcategory": "category || CASE WHEN subcategory <> '' THEN ' / ' || subcategory ELSE '' END AS grp",
        "day": "date AS grp",
        "month": "substr(date, 1, 7) AS grp",
    }[group_by]
    order = "grp" if group_by in ("day", "month") else "total_amount DESC"

    where = "date BETWEEN ? AND ?"
    params = [s, e]
    if category:
        where += " AND category = ? COLLATE NOCASE"
        params.append(normalize_category(category))

    async with db() as c:
        cur = await c.execute(
            f"SELECT {select_expr}, ROUND(SUM(amount), 2) AS total_amount, COUNT(*) AS count "
            f"FROM expenses WHERE {where} GROUP BY {group_exprs[group_by]} ORDER BY {order}",
            params,
        )
        groups = [dict(r) for r in await cur.fetchall()]
    total = round(sum(g["total_amount"] for g in groups), 2)
    return {"status": "success", "range": f"{s} to {e}", "group_by": group_by,
            "total": total, "count": sum(g["count"] for g in groups), "groups": groups}


# --------------------------------------------------------------------------
# Budget tools
# --------------------------------------------------------------------------
@mcp.tool()
@safe
async def set_budget(category: str, amount: float, period: str = "monthly") -> dict:
    """Create or update a budget. category can be a specific category or "All" for
    total spending. period: daily | weekly | monthly | yearly. One budget per category."""
    period = str(period).strip().lower()
    if period not in PERIODS:
        raise ValueError(f"period must be one of: {', '.join(PERIODS)}")
    cat = normalize_budget_category(category)
    amt = parse_amount(amount)
    async with db() as c:
        await c.execute(
            "INSERT INTO budgets(category, amount, period) VALUES (?,?,?) "
            "ON CONFLICT(category) DO UPDATE SET amount = excluded.amount, period = excluded.period",
            (cat, amt, period),
        )
        await c.commit()
        status = await budget_status_for(
            c, {"category": cat, "amount": amt, "period": period}, dt.date.today()
        )
    return {"status": "success", "message": f"{period.capitalize()} budget for {cat} set to {money(amt)}",
            "current_status": status}


@mcp.tool()
@safe
async def list_budgets() -> dict:
    """List all configured budgets."""
    async with db() as c:
        cur = await c.execute("SELECT category, amount, period FROM budgets ORDER BY category")
        rows = [dict(r) for r in await cur.fetchall()]
    return {"status": "success", "count": len(rows), "budgets": rows}


@mcp.tool()
@safe
async def delete_budget(category: str) -> dict:
    """Remove the budget for a category (use "All" for the overall budget)."""
    cat = normalize_budget_category(category)
    async with db() as c:
        cur = await c.execute("DELETE FROM budgets WHERE category = ? COLLATE NOCASE", (cat,))
        await c.commit()
        if cur.rowcount == 0:
            return {"status": "error", "message": f"No budget found for {cat}"}
    return {"status": "success", "message": f"Budget for {cat} removed"}


@mcp.tool()
@safe
async def budget_status(date: Optional[str] = None) -> dict:
    """Show spent / remaining / % used for every budget, in the period containing
    `date` (YYYY-MM-DD, defaults to today)."""
    ref = dt.date.fromisoformat(parse_date(date, default_today=True))
    async with db() as c:
        cur = await c.execute("SELECT category, amount, period FROM budgets ORDER BY category")
        budgets = [dict(r) for r in await cur.fetchall()]
        statuses = [await budget_status_for(c, b, ref) for b in budgets]
    return {"status": "success", "as_of": ref.isoformat(), "count": len(statuses), "budgets": statuses}


# --------------------------------------------------------------------------
# Categories (tool + resource)
# --------------------------------------------------------------------------
@mcp.tool()
async def list_categories() -> dict:
    """List the known expense categories."""
    return {"categories": load_categories()}


@mcp.resource("expense:///categories", mime_type="application/json")
def categories():
    return json.dumps({"categories": load_categories()}, indent=2)


# --------------------------------------------------------------------------
# Start the server
# --------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    # mcp.run()  # use this instead for local stdio