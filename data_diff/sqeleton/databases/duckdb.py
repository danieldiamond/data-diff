from typing import Union

from ..utils import match_regexps
from ..abcs.database_types import (
    Timestamp,
    TimestampTZ,
    DbPath,
    ColType,
    Float,
    Decimal,
    Integer,
    TemporalType,
    Native_UUID,
    Text,
    FractionalType,
    Boolean,
)
from ..abcs.mixins import AbstractMixin_MD5, AbstractMixin_NormalizeValue
from .base import (
    Database,
    BaseDialect,
    import_helper,
    ConnectError,
    ThreadLocalInterpreter,
    TIMESTAMP_PRECISION_POS,
)
from .base import MD5_HEXDIGITS, CHECKSUM_HEXDIGITS, Mixin_Schema


@import_helper("duckdb")
def import_duckdb():
    import duckdb

    return duckdb


class Mixin_MD5(AbstractMixin_MD5):
    def md5_as_int(self, s: str) -> str:
        return f"('0x' || SUBSTRING(md5({s}), {1+MD5_HEXDIGITS-CHECKSUM_HEXDIGITS},{CHECKSUM_HEXDIGITS}))::BIGINT"


class Mixin_NormalizeValue(AbstractMixin_NormalizeValue):
    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        # It's precision 6 by default. If precision is less than 6 -> we remove the trailing numbers.
        if coltype.rounds and coltype.precision > 0:
            return f"CONCAT(SUBSTRING(STRFTIME({value}::TIMESTAMP, '%Y-%m-%d %H:%M:%S.'),1,23), LPAD(((ROUND(strftime({value}::timestamp, '%f')::DECIMAL(15,7)/100000,{coltype.precision-1})*100000)::INT)::VARCHAR,6,'0'))"

        return f"rpad(substring(strftime({value}::timestamp, '%Y-%m-%d %H:%M:%S.%f'),1,{TIMESTAMP_PRECISION_POS+coltype.precision}),26,'0')"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        return self.to_string(f"{value}::DECIMAL(38, {coltype.precision})")

    def normalize_boolean(self, value: str, _coltype: Boolean) -> str:
        return self.to_string(f"{value}::INTEGER")


class Dialect(BaseDialect, Mixin_Schema):
    name = "DuckDB"
    ROUNDS_ON_PREC_LOSS = False
    SUPPORTS_PRIMARY_KEY = True
    SUPPORTS_INDEXES = True

    TYPE_CLASSES = {
        # Timestamps
        "TIMESTAMP WITH TIME ZONE": TimestampTZ,
        "TIMESTAMP": Timestamp,
        # Numbers
        "DOUBLE": Float,
        "FLOAT": Float,
        "DECIMAL": Decimal,
        "INTEGER": Integer,
        "BIGINT": Integer,
        # Text
        "VARCHAR": Text,
        "TEXT": Text,
        # UUID
        "UUID": Native_UUID,
        # Bool
        "BOOLEAN": Boolean,
    }

    def quote(self, s: str):
        return f'"{s}"'

    def to_string(self, s: str):
        return f"{s}::VARCHAR"

    def _convert_db_precision_to_digits(self, p: int) -> int:
        # Subtracting 2 due to wierd precision issues in PostgreSQL
        return super()._convert_db_precision_to_digits(p) - 2

    def parse_type(
        self,
        table_path: DbPath,
        col_name: str,
        type_repr: str,
        datetime_precision: int = None,
        numeric_precision: int = None,
        numeric_scale: int = None,
    ) -> ColType:
        regexps = {
            r"DECIMAL\((\d+),(\d+)\)": Decimal,
        }

        for m, t_cls in match_regexps(regexps, type_repr):
            precision = int(m.group(2))
            return t_cls(precision=precision)

        return super().parse_type(table_path, col_name, type_repr, datetime_precision, numeric_precision, numeric_scale)

    def set_timezone_to_utc(self) -> str:
        return "SET GLOBAL TimeZone='UTC'"

    def current_timestamp(self) -> str:
        return "current_timestamp"


class DuckDB(Database):
    dialect = Dialect()
    SUPPORTS_UNIQUE_CONSTAINT = False  # Temporary, until we implement it
    default_schema = "main"
    CONNECT_URI_HELP = "duckdb://<database>@<dbpath>"
    CONNECT_URI_PARAMS = ["database", "dbpath"]

    def __init__(self, **kw):
        self._args = kw
        self._conn = self.create_connection()

    @property
    def is_autocommit(self) -> bool:
        return True

    def _query(self, sql_code: Union[str, ThreadLocalInterpreter]):
        "Uses the standard SQL cursor interface"
        return self._query_conn(self._conn, sql_code)

    def close(self):
        super().close()
        self._conn.close()

    def create_connection(self):
        ddb = import_duckdb()
        try:
            return ddb.connect(self._args["filepath"])
        except ddb.OperationalError as e:
            raise ConnectError(*e.args) from e
