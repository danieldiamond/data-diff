from dataclasses import field
from datetime import datetime
from typing import Any, Generator, List, Optional, Sequence, Union

from runtype import dataclass

from ..utils import join_iter, ArithString
from ..abcs import Compilable
from ..schema import Schema

from .compiler import Compiler, cv_params, Root
from .base import SKIP, CompileError, DbPath, args_as_tuple


class SqeletonError(Exception):
    pass


class QueryBuilderError(SqeletonError):
    pass


class QB_TypeError(QueryBuilderError):
    pass


class ExprNode(Compilable):
    type: Any = None

    def _dfs_values(self):
        yield self
        for k, vs in dict(self).items():  # __dict__ provided by runtype.dataclass
            if k == "source_table":
                # Skip data-sources, we're only interested in data-parameters
                continue
            if not isinstance(vs, (list, tuple)):
                vs = [vs]
            for v in vs:
                if isinstance(v, ExprNode):
                    yield from v._dfs_values()

    def cast_to(self, to):
        return Cast(self, to)


Expr = Union[ExprNode, str, bool, int, datetime, ArithString, None]


@dataclass
class Code(ExprNode, Root):
    code: str

    def compile(self, c: Compiler) -> str:
        return self.code


def _expr_type(e: Expr) -> type:
    if isinstance(e, ExprNode):
        return e.type
    return type(e)


@dataclass
class Alias(ExprNode):
    expr: Expr
    name: str

    def compile(self, c: Compiler) -> str:
        return f"{c.compile(self.expr)} AS {c.quote(self.name)}"

    @property
    def type(self):
        return _expr_type(self.expr)


def _drop_skips(exprs):
    return [e for e in exprs if e is not SKIP]


def _drop_skips_dict(exprs_dict):
    return {k: v for k, v in exprs_dict.items() if v is not SKIP}


class ITable:
    source_table: Any
    schema: Schema = None

    def select(self, *exprs, distinct=SKIP, **named_exprs):
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        named_exprs = _drop_skips_dict(named_exprs)
        exprs += _named_exprs_as_aliases(named_exprs)
        resolve_names(self.source_table, exprs)
        return Select.make(self, columns=exprs, distinct=distinct)

    def where(self, *exprs):
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.source_table, exprs)
        return Select.make(self, where_exprs=exprs)

    def order_by(self, *exprs):
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.source_table, exprs)
        return Select.make(self, order_by_exprs=exprs)

    def limit(self, limit: int):
        if limit is SKIP:
            return self

        return Select.make(self, limit_expr=limit)

    def at(self, *exprs):
        # TODO
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        raise NotImplementedError()

    def join(self, target):
        return Join(self, target)

    def group_by(self, *, keys=None, values=None):
        keys = _drop_skips(keys)
        resolve_names(self.source_table, keys)

        values = _drop_skips(values)
        resolve_names(self.source_table, values)

        return GroupBy(self, keys, values)

    def with_schema(self):
        # TODO
        raise NotImplementedError()

    def _get_column(self, name: str):
        if self.schema:
            name = self.schema.get_key(name)  # Get the actual name. Might be case-insensitive.
        return Column(self, name)

    # def __getattr__(self, column):
    #     return self._get_column(column)

    def __getitem__(self, column):
        if not isinstance(column, str):
            raise TypeError()
        return self._get_column(column)

    def count(self):
        return Select(self, [Count()])

    def union(self, other: "ITable"):
        return TableOp("UNION", self, other)

    def union_all(self, other: "ITable"):
        return TableOp("UNION ALL", self, other)

    def minus(self, other: "ITable"):
        # aka
        return TableOp("EXCEPT", self, other)

    def intersect(self, other: "ITable"):
        return TableOp("INTERSECT", self, other)


@dataclass
class Concat(ExprNode):
    exprs: list
    sep: str = None

    def compile(self, c: Compiler) -> str:
        # We coalesce because on some DBs (e.g. MySQL) concat('a', NULL) is NULL
        items = [f"coalesce({c.compile(Code(c.dialect.to_string(c.compile(expr))))}, '<null>')" for expr in self.exprs]
        assert items
        if len(items) == 1:
            return items[0]

        if self.sep:
            items = list(join_iter(f"'{self.sep}'", items))
        return c.dialect.concat(items)


@dataclass
class Count(ExprNode):
    expr: Expr = None
    distinct: bool = False

    type = int

    def compile(self, c: Compiler) -> str:
        expr = c.compile(self.expr) if self.expr else "*"
        if self.distinct:
            return f"count(distinct {expr})"

        return f"count({expr})"


class LazyOps:
    def __add__(self, other):
        return BinOp("+", [self, other])

    def __sub__(self, other):
        return BinOp("-", [self, other])

    def __neg__(self):
        return UnaryOp("-", self)

    def __gt__(self, other):
        return BinBoolOp(">", [self, other])

    def __ge__(self, other):
        return BinBoolOp(">=", [self, other])

    def __eq__(self, other):
        if other is None:
            return BinBoolOp("IS", [self, None])
        return BinBoolOp("=", [self, other])

    def __lt__(self, other):
        return BinBoolOp("<", [self, other])

    def __le__(self, other):
        return BinBoolOp("<=", [self, other])

    def __or__(self, other):
        return BinBoolOp("OR", [self, other])

    def __and__(self, other):
        return BinBoolOp("AND", [self, other])

    def is_distinct_from(self, other):
        return IsDistinctFrom(self, other)

    def like(self, other):
        return BinBoolOp("LIKE", [self, other])

    def sum(self):
        return Func("SUM", [self])


@dataclass
class Func(ExprNode, LazyOps):
    name: str
    args: Sequence[Expr]

    def compile(self, c: Compiler) -> str:
        args = ", ".join(c.compile(e) for e in self.args)
        return f"{self.name}({args})"


@dataclass
class WhenThen(ExprNode):
    when: Expr
    then: Expr

    def compile(self, c: Compiler) -> str:
        return f"WHEN {c.compile(self.when)} THEN {c.compile(self.then)}"


@dataclass
class CaseWhen(ExprNode):
    cases: Sequence[WhenThen]
    else_expr: Expr = None

    def compile(self, c: Compiler) -> str:
        assert self.cases
        when_thens = " ".join(c.compile(case) for case in self.cases)
        else_expr = (" ELSE " + c.compile(self.else_expr)) if self.else_expr is not None else ""
        return f"CASE {when_thens}{else_expr} END"

    @property
    def type(self):
        then_types = {_expr_type(case.then) for case in self.cases}
        if self.else_expr:
            then_types |= _expr_type(self.else_expr)
        if len(then_types) > 1:
            raise QB_TypeError(f"Non-matching types in when: {then_types}")
        (t,) = then_types
        return t

    def when(self, *whens: Expr) -> "QB_When":
        whens = args_as_tuple(whens)
        whens = _drop_skips(whens)
        if not whens:
            raise QueryBuilderError("Expected valid whens")

        # XXX reimplementing api.and_()
        if len(whens) == 1:
            return QB_When(self, whens[0])
        return QB_When(self, BinBoolOp("AND", whens))

    def else_(self, then: Expr):
        if self.else_expr is not None:
            raise QueryBuilderError(f"Else clause already specified in {self}")

        return self.replace(else_expr=then)


@dataclass
class QB_When:
    "Partial case-when, used for query-building"
    casewhen: CaseWhen
    when: Expr

    def then(self, then: Expr) -> CaseWhen:
        case = WhenThen(self.when, then)
        return self.casewhen.replace(cases=self.casewhen.cases + [case])


@dataclass(eq=False, order=False)
class IsDistinctFrom(ExprNode, LazyOps):
    a: Expr
    b: Expr
    type = bool

    def compile(self, c: Compiler) -> str:
        return c.dialect.is_distinct_from(c.compile(self.a), c.compile(self.b))


@dataclass(eq=False, order=False)
class BinOp(ExprNode, LazyOps):
    op: str
    args: Sequence[Expr]

    def compile(self, c: Compiler) -> str:
        expr = f" {self.op} ".join(c.compile(a) for a in self.args)
        return f"({expr})"

    @property
    def type(self):
        types = {_expr_type(i) for i in self.args}
        if len(types) > 1:
            raise TypeError(f"Expected all args to have the same type, got {types}")
        (t,) = types
        return t


@dataclass
class UnaryOp(ExprNode, LazyOps):
    op: str
    expr: Expr

    def compile(self, c: Compiler) -> str:
        return f"({self.op}{c.compile(self.expr)})"


class BinBoolOp(BinOp):
    type = bool


@dataclass(eq=False, order=False)
class Column(ExprNode, LazyOps):
    source_table: ITable
    name: str

    @property
    def type(self):
        if self.source_table.schema is None:
            raise QueryBuilderError(f"Schema required for table {self.source_table}")
        return self.source_table.schema[self.name]

    def compile(self, c: Compiler) -> str:
        if c._table_context:
            if len(c._table_context) > 1:
                aliases = [
                    t for t in c._table_context if isinstance(t, TableAlias) and t.source_table is self.source_table
                ]
                if not aliases:
                    return c.quote(self.name)
                elif len(aliases) > 1:
                    raise CompileError(f"Too many aliases for column {self.name}")
                (alias,) = aliases

                return f"{c.quote(alias.name)}.{c.quote(self.name)}"

        return c.quote(self.name)


@dataclass
class TablePath(ExprNode, ITable):
    path: DbPath
    schema: Optional[Schema] = field(default=None, repr=False)

    @property
    def source_table(self):
        return self

    def compile(self, c: Compiler) -> str:
        path = self.path  # c.database._normalize_table_path(self.name)
        return ".".join(map(c.quote, path))

    # Statement shorthands
    def create(self, source_table: ITable = None, *, if_not_exists=False, primary_keys=None):

        if source_table is None and not self.schema:
            raise ValueError("Either schema or source table needed to create table")
        if isinstance(source_table, TablePath):
            source_table = source_table.select()
        return CreateTable(self, source_table, if_not_exists=if_not_exists, primary_keys=primary_keys)

    def drop(self, if_exists=False):
        return DropTable(self, if_exists=if_exists)

    def truncate(self):
        return TruncateTable(self)

    def insert_rows(self, rows, *, columns=None):
        rows = list(rows)
        return InsertToTable(self, ConstantTable(rows), columns=columns)

    def insert_row(self, *values, columns=None):
        return InsertToTable(self, ConstantTable([values]), columns=columns)

    def insert_expr(self, expr: Expr):
        if isinstance(expr, TablePath):
            expr = expr.select()
        return InsertToTable(self, expr)


@dataclass
class TableAlias(ExprNode, ITable):
    source_table: ITable
    name: str

    def compile(self, c: Compiler) -> str:
        return f"{c.compile(self.source_table)} {c.quote(self.name)}"


@dataclass
class Join(ExprNode, ITable, Root):
    source_tables: Sequence[ITable]
    op: str = None
    on_exprs: Sequence[Expr] = None
    columns: Sequence[Expr] = None

    @property
    def source_table(self):
        return self  # TODO is this right?

    @property
    def schema(self):
        assert self.columns  # TODO Implement SELECT *
        s = self.source_tables[0].schema  # XXX
        return type(s)({c.name: c.type for c in self.columns})

    def on(self, *exprs):
        if len(exprs) == 1:
            (e,) = exprs
            if isinstance(e, Generator):
                exprs = tuple(e)

        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        return self.replace(on_exprs=(self.on_exprs or []) + exprs)

    def select(self, *exprs, **named_exprs):
        if self.columns is not None:
            # join-select already applied
            return super().select(*exprs, **named_exprs)

        exprs = _drop_skips(exprs)
        named_exprs = _drop_skips_dict(named_exprs)
        exprs += _named_exprs_as_aliases(named_exprs)
        resolve_names(self.source_table, exprs)
        # TODO Ensure exprs <= self.columns ?
        return self.replace(columns=exprs)

    def compile(self, parent_c: Compiler) -> str:
        tables = [
            t if isinstance(t, TableAlias) else TableAlias(t, parent_c.new_unique_name()) for t in self.source_tables
        ]
        c = parent_c.add_table_context(*tables, in_join=True, in_select=False)
        op = " JOIN " if self.op is None else f" {self.op} JOIN "
        joined = op.join(c.compile(t) for t in tables)

        if self.on_exprs:
            on = " AND ".join(c.compile(e) for e in self.on_exprs)
            res = f"{joined} ON {on}"
        else:
            res = joined

        columns = "*" if self.columns is None else ", ".join(map(c.compile, self.columns))
        select = f"SELECT {columns} FROM {res}"

        if parent_c.in_select:
            select = f"({select}) {c.new_unique_name()}"
        elif parent_c.in_join:
            select = f"({select})"
        return select


@dataclass
class GroupBy(ExprNode, ITable, Root):
    table: ITable
    keys: Sequence[Expr] = None  # IKey?
    values: Sequence[Expr] = None
    having_exprs: Sequence[Expr] = None

    def __post_init__(self):
        assert self.keys or self.values

    def having(self, *exprs):
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.table, exprs)
        return self.replace(having_exprs=(self.having_exprs or []) + exprs)

    def compile(self, c: Compiler) -> str:
        keys = [str(i + 1) for i in range(len(self.keys))]
        columns = (self.keys or []) + (self.values or [])
        if isinstance(self.table, Select) and self.table.columns is None and self.table.group_by_exprs is None:
            return c.compile(
                self.table.replace(
                    columns=columns,
                    group_by_exprs=[Code(k) for k in keys],
                    having_exprs=self.having_exprs,
                )
            )

        keys_str = ", ".join(keys)
        columns_str = ", ".join(c.compile(x) for x in columns)
        having_str = (
            " HAVING " + " AND ".join(map(c.compile, self.having_exprs)) if self.having_exprs is not None else ""
        )
        return (
            f"SELECT {columns_str} FROM {c.replace(in_select=True).compile(self.table)} GROUP BY {keys_str}{having_str}"
        )


@dataclass
class TableOp(ExprNode, ITable, Root):
    op: str
    table1: ITable
    table2: ITable

    @property
    def source_table(self):
        return self  # TODO is this right?

    @property
    def type(self):
        return self.table1.type

    @property
    def schema(self):
        s1 = self.table1.schema
        s2 = self.table2.schema
        assert len(s1) == len(s2)
        return s1

    def compile(self, parent_c: Compiler) -> str:
        c = parent_c.replace(in_select=False)
        table_expr = f"{c.compile(self.table1)} {self.op} {c.compile(self.table2)}"
        if parent_c.in_select:
            table_expr = f"({table_expr}) {c.new_unique_name()}"
        elif parent_c.in_join:
            table_expr = f"({table_expr})"
        return table_expr


@dataclass
class Select(ExprNode, ITable, Root):
    table: Expr = None
    columns: Sequence[Expr] = None
    where_exprs: Sequence[Expr] = None
    order_by_exprs: Sequence[Expr] = None
    group_by_exprs: Sequence[Expr] = None
    having_exprs: Sequence[Expr] = None
    limit_expr: int = None
    distinct: bool = False

    @property
    def schema(self):
        s = self.table.schema
        if s is None or self.columns is None:
            return s
        return type(s)({c.name: c.type for c in self.columns})

    @property
    def source_table(self):
        return self

    def compile(self, parent_c: Compiler) -> str:
        c = parent_c.replace(in_select=True)  # .add_table_context(self.table)

        columns = ", ".join(map(c.compile, self.columns)) if self.columns else "*"
        distinct = "DISTINCT " if self.distinct else ""
        select = f"SELECT {distinct}{columns}"

        if self.table:
            select += " FROM " + c.compile(self.table)
        elif c.dialect.PLACEHOLDER_TABLE:
            select += f" FROM {c.dialect.PLACEHOLDER_TABLE}"

        if self.where_exprs:
            select += " WHERE " + " AND ".join(map(c.compile, self.where_exprs))

        if self.group_by_exprs:
            select += " GROUP BY " + ", ".join(map(c.compile, self.group_by_exprs))

        if self.having_exprs:
            assert self.group_by_exprs
            select += " HAVING " + " AND ".join(map(c.compile, self.having_exprs))

        if self.order_by_exprs:
            select += " ORDER BY " + ", ".join(map(c.compile, self.order_by_exprs))

        if self.limit_expr is not None:
            select += " " + c.dialect.offset_limit(0, self.limit_expr)

        if parent_c.in_select:
            select = f"({select}) {c.new_unique_name()}"
        elif parent_c.in_join:
            select = f"({select})"
        return select

    @classmethod
    def make(cls, table: ITable, distinct: bool = SKIP, **kwargs):
        assert "table" not in kwargs

        if not isinstance(table, cls):  # If not Select
            if distinct is not SKIP:
                kwargs["distinct"] = distinct
            return cls(table, **kwargs)

        # We can safely assume isinstance(table, Select)

        if distinct is not SKIP:
            if distinct == False and table.distinct:
                return cls(table, **kwargs)
            kwargs["distinct"] = distinct

        if table.limit_expr or table.group_by_exprs:
            return cls(table, **kwargs)

        # Fill in missing attributes, instead of creating a new instance.
        for k, v in kwargs.items():
            if getattr(table, k) is not None:
                if k == "where_exprs":  # Additive attribute
                    kwargs[k] = getattr(table, k) + v
                elif k == "distinct":
                    pass
                else:
                    raise ValueError(k)

        return table.replace(**kwargs)


@dataclass
class Cte(ExprNode, ITable):
    source_table: Expr
    name: str = None
    params: Sequence[str] = None

    def compile(self, parent_c: Compiler) -> str:
        c = parent_c.replace(_table_context=[], in_select=False)
        compiled = c.compile(self.source_table)

        name = self.name or parent_c.new_unique_name()
        name_params = f"{name}({', '.join(self.params)})" if self.params else name
        parent_c._subqueries[name_params] = compiled

        return name

    @property
    def schema(self):
        # TODO add cte to schema
        return self.source_table.schema


def _named_exprs_as_aliases(named_exprs):
    return [Alias(expr, name) for name, expr in named_exprs.items()]


def resolve_names(source_table, exprs):
    i = 0
    for expr in exprs:
        # Iterate recursively and update _ResolveColumn instances with the right expression
        if isinstance(expr, ExprNode):
            for v in expr._dfs_values():
                if isinstance(v, _ResolveColumn):
                    v.resolve(source_table._get_column(v.resolve_name))
                    i += 1


@dataclass(frozen=False, eq=False, order=False)
class _ResolveColumn(ExprNode, LazyOps):
    resolve_name: str
    resolved: Expr = None

    def resolve(self, expr: Expr):
        if self.resolved is not None:
            raise QueryBuilderError("Already resolved!")
        self.resolved = expr

    def _get_resolved(self) -> Expr:
        if self.resolved is None:
            raise QueryBuilderError(f"Column not resolved: {self.resolve_name}")
        return self.resolved

    def compile(self, c: Compiler) -> str:
        return self._get_resolved().compile(c)

    @property
    def type(self):
        return self._get_resolved().type

    @property
    def name(self):
        return self._get_resolved().name


class This:
    def __getattr__(self, name):
        return _ResolveColumn(name)

    def __getitem__(self, name):
        if isinstance(name, (list, tuple)):
            return [_ResolveColumn(n) for n in name]
        return _ResolveColumn(name)


@dataclass
class In(ExprNode):
    expr: Expr
    list: Sequence[Expr]

    type = bool

    def compile(self, c: Compiler):
        elems = ", ".join(map(c.compile, self.list))
        return f"({c.compile(self.expr)} IN ({elems}))"


@dataclass
class Cast(ExprNode):
    expr: Expr
    target_type: Expr

    def compile(self, c: Compiler) -> str:
        return f"cast({c.compile(self.expr)} as {c.compile(self.target_type)})"


@dataclass
class Random(ExprNode):
    type = float

    def compile(self, c: Compiler) -> str:
        return c.dialect.random()


@dataclass
class ConstantTable(ExprNode):
    rows: Sequence[Sequence]

    def compile(self, c: Compiler) -> str:
        raise NotImplementedError()

    def compile_for_insert(self, c: Compiler):
        return c.dialect.constant_values(self.rows)


@dataclass
class Explain(ExprNode, Root):
    select: Select

    type = str

    def compile(self, c: Compiler) -> str:
        return c.dialect.explain_as_text(c.compile(self.select))


class CurrentTimestamp(ExprNode):
    type = datetime

    def compile(self, c: Compiler) -> str:
        return c.dialect.current_timestamp()


# DDL


class Statement(Compilable, Root):
    type = None


@dataclass
class CreateTable(Statement):
    path: TablePath
    source_table: Expr = None
    if_not_exists: bool = False
    primary_keys: List[str] = None

    def compile(self, c: Compiler) -> str:
        ne = "IF NOT EXISTS " if self.if_not_exists else ""
        if self.source_table:
            return f"CREATE TABLE {ne}{c.compile(self.path)} AS {c.compile(self.source_table)}"

        schema = ", ".join(f"{c.dialect.quote(k)} {c.dialect.type_repr(v)}" for k, v in self.path.schema.items())
        pks = (
            ", PRIMARY KEY (%s)" % ", ".join(self.primary_keys)
            if self.primary_keys and c.dialect.SUPPORTS_PRIMARY_KEY
            else ""
        )
        return f"CREATE TABLE {ne}{c.compile(self.path)}({schema}{pks})"


@dataclass
class DropTable(Statement):
    path: TablePath
    if_exists: bool = False

    def compile(self, c: Compiler) -> str:
        ie = "IF EXISTS " if self.if_exists else ""
        return f"DROP TABLE {ie}{c.compile(self.path)}"


@dataclass
class TruncateTable(Statement):
    path: TablePath

    def compile(self, c: Compiler) -> str:
        return f"TRUNCATE TABLE {c.compile(self.path)}"


@dataclass
class InsertToTable(Statement):
    # TODO Support insert for only some columns
    path: TablePath
    expr: Expr
    columns: List[str] = None

    def compile(self, c: Compiler) -> str:
        if isinstance(self.expr, ConstantTable):
            expr = self.expr.compile_for_insert(c)
        else:
            expr = c.compile(self.expr)

        columns = "(%s)" % ", ".join(map(c.quote, self.columns)) if self.columns is not None else ""

        return f"INSERT INTO {c.compile(self.path)}{columns} {expr}"


@dataclass
class Commit(Statement):
    def compile(self, c: Compiler) -> str:
        return "COMMIT" if not c.database.is_autocommit else SKIP


@dataclass
class Param(ExprNode, ITable):
    """A value placeholder, to be specified at compilation time using the `cv_params` context variable."""

    name: str

    @property
    def source_table(self):
        return self

    def compile(self, c: Compiler) -> str:
        params = cv_params.get()
        return c._compile(params[self.name])
