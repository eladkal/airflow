#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Jinja2 template rendering context helper."""

from __future__ import annotations

import contextlib
import copy
import functools
import warnings
from collections.abc import (
    Container,
    ItemsView,
    Iterator,
    KeysView,
    Mapping,
    MutableMapping,
    ValuesView,
)
from typing import (
    TYPE_CHECKING,
    Any,
    SupportsIndex,
    Union,
)

import attrs
import lazy_object_proxy
from sqlalchemy import and_, select

from airflow.exceptions import RemovedInAirflow3Warning
from airflow.models.asset import (
    AssetAliasModel,
    AssetEvent,
    AssetModel,
    fetch_active_assets_by_name,
    fetch_active_assets_by_uri,
)
from airflow.sdk.definitions.asset import (
    Asset,
    AssetAlias,
    AssetAliasUniqueKey,
    AssetNameRef,
    AssetRef,
    AssetUniqueKey,
    AssetUriRef,
    BaseAssetUniqueKey,
)
from airflow.utils.db import LazySelectSequence
from airflow.utils.session import create_session
from airflow.utils.types import NOTSET

if TYPE_CHECKING:
    from sqlalchemy.engine import Row
    from sqlalchemy.orm import Session
    from sqlalchemy.sql.expression import Select, TextClause

    from airflow.models.baseoperator import BaseOperator

# NOTE: Please keep this in sync with the following:
# * Context in airflow/utils/context.pyi.
# * Table in docs/apache-airflow/templates-ref.rst
KNOWN_CONTEXT_KEYS: set[str] = {
    "conn",
    "dag",
    "dag_run",
    "data_interval_end",
    "data_interval_start",
    "ds",
    "ds_nodash",
    "expanded_ti_count",
    "exception",
    "inlets",
    "inlet_events",
    "logical_date",
    "macros",
    "map_index_template",
    "outlets",
    "outlet_events",
    "params",
    "prev_data_interval_start_success",
    "prev_data_interval_end_success",
    "prev_start_date_success",
    "prev_end_date_success",
    "reason",
    "run_id",
    "task",
    "task_instance",
    "task_instance_key_str",
    "test_mode",
    "templates_dict",
    "ti",
    "triggering_asset_events",
    "ts",
    "ts_nodash",
    "ts_nodash_with_tz",
    "try_number",
    "var",
}


class VariableAccessor:
    """Wrapper to access Variable values in template."""

    def __init__(self, *, deserialize_json: bool) -> None:
        self._deserialize_json = deserialize_json
        self.var: Any = None

    def __getattr__(self, key: str) -> Any:
        from airflow.models.variable import Variable

        self.var = Variable.get(key, deserialize_json=self._deserialize_json)
        return self.var

    def __repr__(self) -> str:
        return str(self.var)

    def get(self, key, default: Any = NOTSET) -> Any:
        from airflow.models.variable import Variable

        if default is NOTSET:
            return Variable.get(key, deserialize_json=self._deserialize_json)
        return Variable.get(key, default, deserialize_json=self._deserialize_json)


class ConnectionAccessor:
    """Wrapper to access Connection entries in template."""

    def __init__(self) -> None:
        self.var: Any = None

    def __getattr__(self, key: str) -> Any:
        from airflow.models.connection import Connection

        self.var = Connection.get_connection_from_secrets(key)
        return self.var

    def __repr__(self) -> str:
        return str(self.var)

    def get(self, key: str, default_conn: Any = None) -> Any:
        from airflow.exceptions import AirflowNotFoundException
        from airflow.models.connection import Connection

        try:
            return Connection.get_connection_from_secrets(key)
        except AirflowNotFoundException:
            return default_conn


@attrs.define()
class AssetAliasEvent:
    """
    Represeation of asset event to be triggered by an asset alias.

    :meta private:
    """

    source_alias_name: str
    dest_asset_key: AssetUniqueKey
    extra: dict[str, Any]


@attrs.define()
class OutletEventAccessor:
    """
    Wrapper to access an outlet asset event in template.

    :meta private:
    """

    key: BaseAssetUniqueKey
    extra: dict[str, Any] = attrs.Factory(dict)
    asset_alias_events: list[AssetAliasEvent] = attrs.field(factory=list)

    def add(self, asset: Asset, extra: dict[str, Any] | None = None) -> None:
        """Add an AssetEvent to an existing Asset."""
        if not isinstance(self.key, AssetAliasUniqueKey):
            return

        asset_alias_name = self.key.name
        event = AssetAliasEvent(
            source_alias_name=asset_alias_name,
            dest_asset_key=AssetUniqueKey.from_asset(asset),
            extra=extra or {},
        )
        self.asset_alias_events.append(event)


class OutletEventAccessors(Mapping[Union[Asset, AssetAlias], OutletEventAccessor]):
    """
    Lazy mapping of outlet asset event accessors.

    :meta private:
    """

    _asset_ref_cache: dict[AssetRef, AssetUniqueKey] = {}

    def __init__(self) -> None:
        self._dict: dict[BaseAssetUniqueKey, OutletEventAccessor] = {}

    def __str__(self) -> str:
        return f"OutletEventAccessors(_dict={self._dict})"

    def __iter__(self) -> Iterator[Asset | AssetAlias]:
        return (
            key.to_asset() if isinstance(key, AssetUniqueKey) else key.to_asset_alias() for key in self._dict
        )

    def __len__(self) -> int:
        return len(self._dict)

    def __getitem__(self, key: Asset | AssetAlias) -> OutletEventAccessor:
        hashable_key: BaseAssetUniqueKey
        if isinstance(key, Asset):
            hashable_key = AssetUniqueKey.from_asset(key)
        elif isinstance(key, AssetAlias):
            hashable_key = AssetAliasUniqueKey.from_asset_alias(key)
        elif isinstance(key, AssetRef):
            hashable_key = self._resolve_asset_ref(key)
        else:
            raise TypeError(f"Key should be either an asset or an asset alias, not {type(key)}")

        if hashable_key not in self._dict:
            self._dict[hashable_key] = OutletEventAccessor(extra={}, key=hashable_key)
        return self._dict[hashable_key]

    def _resolve_asset_ref(self, ref: AssetRef) -> AssetUniqueKey:
        with contextlib.suppress(KeyError):
            return self._asset_ref_cache[ref]

        refs_to_cache: list[AssetRef]
        with create_session() as session:
            if isinstance(ref, AssetNameRef):
                asset = session.scalar(
                    select(AssetModel).where(AssetModel.name == ref.name, AssetModel.active.has())
                )
                refs_to_cache = [ref, AssetUriRef(asset.uri)]
            elif isinstance(ref, AssetUriRef):
                asset = session.scalar(
                    select(AssetModel).where(AssetModel.uri == ref.uri, AssetModel.active.has())
                )
                refs_to_cache = [ref, AssetNameRef(asset.name)]
            else:
                raise TypeError(f"Unimplemented asset ref: {type(ref)}")
            for ref in refs_to_cache:
                self._asset_ref_cache[ref] = unique_key = AssetUniqueKey.from_asset(asset)
        return unique_key


class LazyAssetEventSelectSequence(LazySelectSequence[AssetEvent]):
    """
    List-like interface to lazily access AssetEvent rows.

    :meta private:
    """

    @staticmethod
    def _rebuild_select(stmt: TextClause) -> Select:
        return select(AssetEvent).from_statement(stmt)

    @staticmethod
    def _process_row(row: Row) -> AssetEvent:
        return row[0]


@attrs.define(init=False)
class InletEventsAccessors(Mapping[Union[int, Asset, AssetAlias, AssetRef], LazyAssetEventSelectSequence]):
    """
    Lazy mapping for inlet asset events accessors.

    :meta private:
    """

    _inlets: list[Any]
    _assets: dict[AssetUniqueKey, Asset]
    _asset_aliases: dict[AssetAliasUniqueKey, AssetAlias]
    _session: Session

    def __init__(self, inlets: list, *, session: Session) -> None:
        self._inlets = inlets
        self._session = session
        self._assets = {}
        self._asset_aliases = {}

        _asset_ref_names: list[str] = []
        _asset_ref_uris: list[str] = []
        for inlet in inlets:
            if isinstance(inlet, Asset):
                self._assets[AssetUniqueKey.from_asset(inlet)] = inlet
            elif isinstance(inlet, AssetAlias):
                self._asset_aliases[AssetAliasUniqueKey.from_asset_alias(inlet)] = inlet
            elif isinstance(inlet, AssetNameRef):
                _asset_ref_names.append(inlet.name)
            elif isinstance(inlet, AssetUriRef):
                _asset_ref_uris.append(inlet.uri)

        if _asset_ref_names:
            for _, asset in fetch_active_assets_by_name(_asset_ref_names, self._session).items():
                self._assets[AssetUniqueKey.from_asset(asset)] = asset
        if _asset_ref_uris:
            for _, asset in fetch_active_assets_by_uri(_asset_ref_uris, self._session).items():
                self._assets[AssetUniqueKey.from_asset(asset)] = asset

    def __iter__(self) -> Iterator[Asset | AssetAlias]:
        return iter(self._inlets)

    def __len__(self) -> int:
        return len(self._inlets)

    def __getitem__(self, key: int | Asset | AssetAlias | AssetRef) -> LazyAssetEventSelectSequence:
        if isinstance(key, int):  # Support index access; it's easier for trivial cases.
            obj = self._inlets[key]
            if not isinstance(obj, (Asset, AssetAlias, AssetRef)):
                raise IndexError(key)
        else:
            obj = key

        if isinstance(obj, Asset):
            asset = self._assets[AssetUniqueKey.from_asset(obj)]
            join_clause = AssetEvent.asset
            where_clause = and_(AssetModel.name == asset.name, AssetModel.uri == asset.uri)
        elif isinstance(obj, AssetAlias):
            asset_alias = self._asset_aliases[AssetAliasUniqueKey.from_asset_alias(obj)]
            join_clause = AssetEvent.source_aliases
            where_clause = AssetAliasModel.name == asset_alias.name
        elif isinstance(obj, AssetNameRef):
            try:
                asset = next(a for k, a in self._assets.items() if k.name == obj.name)
            except StopIteration:
                raise KeyError(obj) from None
            join_clause = AssetEvent.asset
            where_clause = and_(AssetModel.name == asset.name, AssetModel.active.has())
        elif isinstance(obj, AssetUriRef):
            try:
                asset = next(a for k, a in self._assets.items() if k.uri == obj.uri)
            except StopIteration:
                raise KeyError(obj) from None
            join_clause = AssetEvent.asset
            where_clause = and_(AssetModel.uri == asset.uri, AssetModel.active.has())
        else:
            raise ValueError(key)

        return LazyAssetEventSelectSequence.from_select(
            select(AssetEvent).join(join_clause).where(where_clause),
            order_by=[AssetEvent.timestamp],
            session=self._session,
        )


class AirflowContextDeprecationWarning(RemovedInAirflow3Warning):
    """Warn for usage of deprecated context variables in a task."""


def _create_deprecation_warning(key: str, replacements: list[str]) -> RemovedInAirflow3Warning:
    message = f"Accessing {key!r} from the template is deprecated and will be removed in a future version."
    if not replacements:
        return AirflowContextDeprecationWarning(message)
    display_except_last = ", ".join(repr(r) for r in replacements[:-1])
    if display_except_last:
        message += f" Please use {display_except_last} or {replacements[-1]!r} instead."
    else:
        message += f" Please use {replacements[-1]!r} instead."
    return AirflowContextDeprecationWarning(message)


class Context(MutableMapping[str, Any]):
    """
    Jinja2 template context for task rendering.

    This is a mapping (dict-like) class that can lazily emit warnings when
    (and only when) deprecated context keys are accessed.
    """

    _DEPRECATION_REPLACEMENTS: dict[str, list[str]] = {}

    def __init__(self, context: MutableMapping[str, Any] | None = None, **kwargs: Any) -> None:
        self._context: MutableMapping[str, Any] = context or {}
        if kwargs:
            self._context.update(kwargs)
        self._deprecation_replacements = self._DEPRECATION_REPLACEMENTS.copy()

    def __repr__(self) -> str:
        return repr(self._context)

    def __reduce_ex__(self, protocol: SupportsIndex) -> tuple[Any, ...]:
        """
        Pickle the context as a dict.

        We are intentionally going through ``__getitem__`` in this function,
        instead of using ``items()``, to trigger deprecation warnings.
        """
        items = [(key, self[key]) for key in self._context]
        return dict, (items,)

    def __copy__(self) -> Context:
        new = type(self)(copy.copy(self._context))
        new._deprecation_replacements = self._deprecation_replacements.copy()
        return new

    def __getitem__(self, key: str) -> Any:
        with contextlib.suppress(KeyError):
            warnings.warn(
                _create_deprecation_warning(key, self._deprecation_replacements[key]),
                stacklevel=2,
            )
        with contextlib.suppress(KeyError):
            return self._context[key]
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self._deprecation_replacements.pop(key, None)
        self._context[key] = value

    def __delitem__(self, key: str) -> None:
        self._deprecation_replacements.pop(key, None)
        del self._context[key]

    def __contains__(self, key: object) -> bool:
        return key in self._context

    def __iter__(self) -> Iterator[str]:
        return iter(self._context)

    def __len__(self) -> int:
        return len(self._context)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Context):
            return NotImplemented
        return self._context == other._context

    def __ne__(self, other: Any) -> bool:
        if not isinstance(other, Context):
            return NotImplemented
        return self._context != other._context

    def keys(self) -> KeysView[str]:
        return self._context.keys()

    def items(self):
        return ItemsView(self._context)

    def values(self):
        return ValuesView(self._context)


def context_merge(context: Mapping[str, Any], *args: Any, **kwargs: Any) -> None:
    """
    Merge parameters into an existing context.

    Like ``dict.update()`` , this take the same parameters, and updates
    ``context`` in-place.

    This is implemented as a free function because the ``Context`` type is
    "faked" as a ``TypedDict`` in ``context.pyi``, which cannot have custom
    functions.

    :meta private:
    """
    if not context:
        context = Context()

    context.update(*args, **kwargs)


def context_update_for_unmapped(context: Mapping[str, Any], task: BaseOperator) -> None:
    """
    Update context after task unmapping.

    Since ``get_template_context()`` is called before unmapping, the context
    contains information about the mapped task. We need to do some in-place
    updates to ensure the template context reflects the unmapped task instead.

    :meta private:
    """
    from airflow.models.param import process_params

    context["task"] = context["ti"].task = task
    context["params"] = process_params(context["dag"], task, context["dag_run"], suppress_exception=False)


def context_copy_partial(source: Mapping[str, Any], keys: Container[str]) -> Context:
    """
    Create a context by copying items under selected keys in ``source``.

    This is implemented as a free function because the ``Context`` type is
    "faked" as a ``TypedDict`` in ``context.pyi``, which cannot have custom
    functions.

    :meta private:
    """
    new = Context({k: v for k, v in source._context.items() if k in keys})
    new._deprecation_replacements = source._deprecation_replacements.copy()
    return new


def lazy_mapping_from_context(source: Context) -> Mapping[str, Any]:
    """
    Create a mapping that wraps deprecated entries in a lazy object proxy.

    This further delays deprecation warning to until when the entry is actually
    used, instead of when it's accessed in the context. The result is useful for
    passing into a callable with ``**kwargs``, which would unpack the mapping
    too eagerly otherwise.

    This is implemented as a free function because the ``Context`` type is
    "faked" as a ``TypedDict`` in ``context.pyi``, which cannot have custom
    functions.

    :meta private:
    """
    if not isinstance(source, Context):
        # Sometimes we are passed a plain dict (usually in tests, or in User's
        # custom operators) -- be lenient about what we accept so we don't
        # break anything for users.
        return source

    def _deprecated_proxy_factory(k: str, v: Any) -> Any:
        replacements = source._deprecation_replacements[k]
        warnings.warn(_create_deprecation_warning(k, replacements), stacklevel=2)
        return v

    def _create_value(k: str, v: Any) -> Any:
        if k not in source._deprecation_replacements:
            return v
        factory = functools.partial(_deprecated_proxy_factory, k, v)
        return lazy_object_proxy.Proxy(factory)

    return {k: _create_value(k, v) for k, v in source._context.items()}


def context_get_outlet_events(context: Context) -> OutletEventAccessors:
    try:
        return context["outlet_events"]
    except KeyError:
        return OutletEventAccessors()
