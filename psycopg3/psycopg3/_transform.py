"""
Helper object to transform values between Python and PostgreSQL
"""

# Copyright (C) 2020 The Psycopg Team

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
from typing import cast, TYPE_CHECKING

from . import errors as e
from .pq import Format
from .oids import INVALID_OID, TEXT_OID
from .proto import LoadFunc, AdaptContext

if TYPE_CHECKING:
    from .pq.proto import PGresult
    from .adapt import Dumper, Loader, AdaptersMap
    from .connection import BaseConnection
    from .types.array import BaseListDumper

DumperKey = Union[type, Tuple[type, type]]
DumperCache = Dict[DumperKey, "Dumper"]

LoaderKey = int
LoaderCache = Dict[LoaderKey, "Loader"]


class Transformer(AdaptContext):
    """
    An object that can adapt efficiently between Python and PostgreSQL.

    The life cycle of the object is the query, so it is assumed that stuff like
    the server version or connection encoding will not change. It can have its
    state so adapting several values of the same type can be optimised.
    """

    __module__ = "psycopg3.adapt"
    _adapters: "AdaptersMap"
    _pgresult: Optional["PGresult"] = None

    def __init__(self, context: Optional[AdaptContext] = None):
        self._unknown_oid = INVALID_OID

        # WARNING: don't store context, or you'll create a loop with the Cursor
        if context:
            self._adapters = context.adapters
            conn = self._connection = context.connection

            # PG 9.6 gives an error if an unknown oid is emitted as column
            if conn and conn.pgconn.server_version < 100000:
                self._unknown_oid = TEXT_OID
        else:
            from .adapt import global_adapters

            self._adapters = global_adapters
            self._connection = None

        # mapping class, fmt -> Dumper instance
        self._dumpers_cache: Tuple[DumperCache, DumperCache] = ({}, {})

        # mapping oid, fmt -> Loader instance
        self._loaders_cache: Tuple[LoaderCache, LoaderCache] = ({}, {})

        # sequence of load functions from value to python
        # the length of the result columns
        self._row_loaders: List[LoadFunc] = []

    @property
    def connection(self) -> Optional["BaseConnection"]:
        return self._connection

    @property
    def adapters(self) -> "AdaptersMap":
        return self._adapters

    @property
    def pgresult(self) -> Optional["PGresult"]:
        return self._pgresult

    @pgresult.setter
    def pgresult(self, result: Optional["PGresult"]) -> None:
        self._pgresult = result
        rc = self._row_loaders = []

        self._ntuples: int
        self._nfields: int
        if not result:
            self._nfields = self._ntuples = 0
            return

        nf = self._nfields = result.nfields
        self._ntuples = result.ntuples

        for i in range(nf):
            oid = result.ftype(i)
            fmt = result.fformat(i)
            rc.append(self.get_loader(oid, fmt).load)  # type: ignore

    def set_row_types(
        self, types: Sequence[int], formats: Sequence[Format]
    ) -> None:
        rc: List[LoadFunc] = [None] * len(types)  # type: ignore[list-item]
        for i in range(len(rc)):
            rc[i] = self.get_loader(types[i], formats[i]).load

        self._row_loaders = rc

    def dump_sequence(
        self, params: Sequence[Any], formats: Sequence[Format]
    ) -> Tuple[List[Any], Tuple[int, ...]]:
        ps: List[Optional[bytes]] = [None] * len(params)
        ts = [self._unknown_oid] * len(params)
        for i in range(len(params)):
            param = params[i]
            if param is not None:
                dumper = self.get_dumper(param, formats[i])
                ps[i] = dumper.dump(param)
                ts[i] = dumper.oid

        return ps, tuple(ts)

    def get_dumper(self, obj: Any, format: Format) -> "Dumper":
        # Fast path: return a Dumper class already instantiated from the same type
        cls = type(obj)
        if cls is not list:
            key: DumperKey = cls
        else:
            # TODO: Can be probably generalised to handle other recursive types
            subobj = self._find_list_element(obj)
            if subobj is None:
                subobj = ""
            key = (cls, type(subobj))

        try:
            return self._dumpers_cache[format][key]
        except KeyError:
            pass

        dcls = self._adapters.get_dumper(cls, format)
        if not dcls:
            raise e.ProgrammingError(
                f"cannot adapt type {cls.__name__}"
                f" to format {Format(format).name}"
            )

        d = self._dumpers_cache[format][key] = dcls(cls, self)
        if cls is list:
            sub_dumper = self.get_dumper(subobj, format)
            cast("BaseListDumper", d).set_sub_dumper(sub_dumper)

        return d

    def load_rows(self, row0: int, row1: int) -> List[Tuple[Any, ...]]:
        res = self._pgresult
        if not res:
            raise e.InterfaceError("result not set")

        if not (0 <= row0 <= self._ntuples and 0 <= row1 <= self._ntuples):
            raise e.InterfaceError(
                f"rows must be included between 0 and {self._ntuples}"
            )

        records: List[Tuple[Any, ...]]
        records = [None] * (row1 - row0)  # type: ignore[list-item]
        for row in range(row0, row1):
            record: List[Any] = [None] * self._nfields
            for col in range(self._nfields):
                val = res.get_value(row, col)
                if val is not None:
                    record[col] = self._row_loaders[col](val)
            records[row - row0] = tuple(record)

        return records

    def load_row(self, row: int) -> Optional[Tuple[Any, ...]]:
        res = self._pgresult
        if not res:
            return None

        if not 0 <= row < self._ntuples:
            return None

        record: List[Any] = [None] * self._nfields
        for col in range(self._nfields):
            val = res.get_value(row, col)
            if val is not None:
                record[col] = self._row_loaders[col](val)

        return tuple(record)

    def load_sequence(
        self, record: Sequence[Optional[bytes]]
    ) -> Tuple[Any, ...]:
        if len(self._row_loaders) != len(record):
            raise e.ProgrammingError(
                f"cannot load sequence of {len(record)} items:"
                f" {len(self._row_loaders)} loaders registered"
            )

        return tuple(
            (self._row_loaders[i](val) if val is not None else None)
            for i, val in enumerate(record)
        )

    def get_loader(self, oid: int, format: Format) -> "Loader":
        try:
            return self._loaders_cache[format][oid]
        except KeyError:
            pass

        loader_cls = self._adapters.get_loader(oid, format)
        if not loader_cls:
            loader_cls = self._adapters.get_loader(INVALID_OID, format)
            if not loader_cls:
                raise e.InterfaceError("unknown oid loader not found")
        loader = self._loaders_cache[format][oid] = loader_cls(oid, self)
        return loader

    def _find_list_element(
        self, L: List[Any], seen: Optional[Set[int]] = None
    ) -> Any:
        """
        Find the first non-null element of an eventually nested list
        """
        if not seen:
            seen = set()
        if id(L) in seen:
            raise e.DataError("cannot dump a recursive list")

        seen.add(id(L))

        for it in L:
            if type(it) is list:
                subit = self._find_list_element(it, seen)
                if subit is not None:
                    return subit
            elif it is not None:
                return it

        return None
