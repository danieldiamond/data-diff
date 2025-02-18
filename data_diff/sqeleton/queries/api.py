from typing import Optional

from ..utils import CaseAwareMapping, CaseSensitiveDict
from .ast_classes import *
from .base import args_as_tuple


this = This()


def join(*tables: ITable):
    "Joins each table into a 'struct'"
    return Join(tables)


def leftjoin(*tables: ITable):
    "Left-joins each table into a 'struct'"
    return Join(tables, "LEFT")


def rightjoin(*tables: ITable):
    "Right-joins each table into a 'struct'"
    return Join(tables, "RIGHT")


def outerjoin(*tables: ITable):
    "Outer-joins each table into a 'struct'"
    return Join(tables, "FULL OUTER")


def cte(expr: Expr, *, name: Optional[str] = None, params: Sequence[str] = None):
    return Cte(expr, name, params)


def table(*path: str, schema: Union[dict, CaseAwareMapping] = None) -> TablePath:
    if len(path) == 1 and isinstance(path[0], tuple):
        (path,) = path
    if not all(isinstance(i, str) for i in path):
        raise TypeError(f"All elements of table path must be of type 'str'. Got: {path}")
    if schema and not isinstance(schema, CaseAwareMapping):
        assert isinstance(schema, dict)
        schema = CaseSensitiveDict(schema)
    return TablePath(path, schema)


def or_(*exprs: Expr):
    exprs = args_as_tuple(exprs)
    if len(exprs) == 1:
        return exprs[0]
    return BinBoolOp("OR", exprs)


def and_(*exprs: Expr):
    exprs = args_as_tuple(exprs)
    if len(exprs) == 1:
        return exprs[0]
    return BinBoolOp("AND", exprs)


def sum_(expr: Expr):
    return Func("sum", [expr])


def avg(expr: Expr):
    return Func("avg", [expr])


def min_(expr: Expr):
    return Func("min", [expr])


def max_(expr: Expr):
    return Func("max", [expr])


def if_(cond: Expr, then: Expr, else_: Optional[Expr] = None):
    return when(cond).then(then).else_(else_)


def when(*when_exprs: Expr):
    return CaseWhen([]).when(*when_exprs)


def coalesce(*exprs):
    exprs = args_as_tuple(exprs)
    return Func("COALESCE", exprs)


def insert_rows_in_batches(db, tbl: TablePath, rows, *, columns=None, batch_size=1024 * 8):
    assert batch_size > 0
    rows = list(rows)

    while rows:
        batch, rows = rows[:batch_size], rows[batch_size:]
        db.query(tbl.insert_rows(batch, columns=columns))


def current_timestamp():
    return CurrentTimestamp()


commit = Commit()
