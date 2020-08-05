"""
Entry point into the adaptation system.
"""

# Copyright (C) 2020 The Psycopg Team

from typing import Any, Callable, Optional, Tuple, Type, Union

from . import pq
from . import proto
from .pq import Format as Format
from .proto import AdaptContext, DumpersMap, DumperType
from .proto import LoadersMap, LoaderType
from .cursor import BaseCursor
from .connection import BaseConnection


class Dumper:
    globals: DumpersMap = {}
    connection: Optional[BaseConnection]

    def __init__(self, src: type, context: AdaptContext = None):
        self.src = src
        self.context = context
        self.connection = _connection_from_context(context)

    def dump(self, obj: Any) -> Union[bytes, Tuple[bytes, int]]:
        raise NotImplementedError()

    @classmethod
    def register(
        cls,
        src: type,
        dumper: DumperType,
        context: AdaptContext = None,
        format: Format = Format.TEXT,
    ) -> DumperType:
        if not isinstance(src, type):
            raise TypeError(
                f"dumpers should be registered on classes, got {src} instead"
            )

        if not (
            callable(dumper)
            or (isinstance(dumper, type) and issubclass(dumper, Dumper))
        ):
            raise TypeError(
                f"dumpers should be callable or Dumper subclasses,"
                f" got {dumper} instead"
            )

        where = context.dumpers if context is not None else Dumper.globals
        where[src, format] = dumper
        return dumper

    @classmethod
    def register_binary(
        cls, src: type, dumper: DumperType, context: AdaptContext = None,
    ) -> DumperType:
        return cls.register(src, dumper, context, format=Format.BINARY)

    @classmethod
    def text(cls, src: type) -> Callable[[DumperType], DumperType]:
        def text_(dumper: DumperType) -> DumperType:
            cls.register(src, dumper)
            return dumper

        return text_

    @classmethod
    def binary(cls, src: type) -> Callable[[DumperType], DumperType]:
        def binary_(dumper: DumperType) -> DumperType:
            cls.register_binary(src, dumper)
            return dumper

        return binary_


class Loader:
    globals: LoadersMap = {}
    connection: Optional[BaseConnection]

    def __init__(self, oid: int, context: AdaptContext = None):
        self.oid = oid
        self.context = context
        self.connection = _connection_from_context(context)

    def load(self, data: bytes) -> Any:
        raise NotImplementedError()

    @classmethod
    def register(
        cls,
        oid: int,
        loader: LoaderType,
        context: AdaptContext = None,
        format: Format = Format.TEXT,
    ) -> LoaderType:
        if not isinstance(oid, int):
            raise TypeError(
                f"typeloaders should be registered on oid, got {oid} instead"
            )

        if not (
            callable(loader)
            or (isinstance(loader, type) and issubclass(loader, Loader))
        ):
            raise TypeError(
                f"dumpers should be callable or Loader subclasses,"
                f" got {loader} instead"
            )

        where = context.loaders if context is not None else Loader.globals
        where[oid, format] = loader
        return loader

    @classmethod
    def register_binary(
        cls, oid: int, loader: LoaderType, context: AdaptContext = None,
    ) -> LoaderType:
        return cls.register(oid, loader, context, format=Format.BINARY)

    @classmethod
    def text(cls, oid: int) -> Callable[[LoaderType], LoaderType]:
        def text_(loader: LoaderType) -> LoaderType:
            cls.register(oid, loader)
            return loader

        return text_

    @classmethod
    def binary(cls, oid: int) -> Callable[[LoaderType], LoaderType]:
        def binary_(loader: LoaderType) -> LoaderType:
            cls.register_binary(oid, loader)
            return loader

        return binary_


def _connection_from_context(
    context: AdaptContext,
) -> Optional[BaseConnection]:
    if context is None:
        return None
    elif isinstance(context, BaseConnection):
        return context
    elif isinstance(context, BaseCursor):
        return context.connection
    elif isinstance(context, Transformer):
        return context.connection
    else:
        raise TypeError(f"can't get a connection from {type(context)}")


Transformer: Type[proto.Transformer]

# Override it with fast object if available
if pq.__impl__ == "c":
    from psycopg3_c import _psycopg3

    Transformer = _psycopg3.Transformer
else:
    from . import transform

    Transformer = transform.Transformer