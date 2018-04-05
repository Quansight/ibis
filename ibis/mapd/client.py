import ibis
import regex as re
import time
import collections
import datetime

import six

import pandas as pd
import pymapd

from multipledispatch import Dispatcher

# import ibis
import ibis.common as com
import ibis.expr.types as ir
import ibis.expr.schema as sch
import ibis.expr.datatypes as dt

from ibis.compat import parse_version
from ibis.client import Database, Query, SQLClient
from ibis.mapd import compiler as comp

# from google.api.core.exceptions import BadRequest


NATIVE_PARTITION_COL = '_PARTITIONTIME'


def _ensure_split(table_id, dataset_id):
    split = table_id.split('.')
    if len(split) > 1:
        assert len(split) == 2
        if dataset_id:
            raise ValueError(
                "Can't pass a fully qualified table name *AND* a dataset_id"
            )
        (dataset_id, table_id) = split
    return (table_id, dataset_id)


_IBIS_TYPE_TO_DTYPE = {
    'string': 'STRING',
    'int64': 'INT64',
    'double': 'FLOAT64',
    'boolean': 'BOOL',
    'timestamp': 'TIMESTAMP',
    'date': 'DATE',
}

_DTYPE_TO_IBIS_TYPE = {
    'INT64': dt.int64,
    'FLOAT64': dt.double,
    'BOOL': dt.boolean,
    'STRING': dt.string,
    'DATE': dt.date,
    # FIXME: enforce no tz info
    'DATETIME': dt.timestamp,
    'TIME': dt.time,
    'TIMESTAMP': dt.timestamp,
    'BYTES': dt.binary,
}


_LEGACY_TO_STANDARD = {
    'INTEGER': 'INT64',
    'FLOAT': 'FLOAT64',
    'BOOLEAN': 'BOOL',
}


# @dt.dtype.register(pymapd.schema.SchemaField)
def pymapd_field_to_ibis_dtype(field):
    typ = field.field_type
    if typ == 'RECORD':
        fields = field.fields
        assert fields
        names = [el.name for el in fields]
        ibis_types = list(map(dt.dtype, fields))
        ibis_type = dt.Struct(names, ibis_types)
    else:
        ibis_type = _LEGACY_TO_STANDARD.get(typ, typ)
        ibis_type = _DTYPE_TO_IBIS_TYPE.get(ibis_type, ibis_type)
    if field.mode == 'REPEATED':
        ibis_type = dt.Array(ibis_type)
    return ibis_type


# @sch.infer.register(pymapd.table.Table)
def pymapd_schema(table):
    pairs = [(el.name, dt.dtype(el)) for el in table.schema]
    try:
        if table.list_partitions():
            pairs.append((NATIVE_PARTITION_COL, dt.timestamp))
    except Exception:
        pass
    return sch.schema(pairs)


class MapDCursor(object):
    """Cursor to allow the MapD client to reuse machinery in ibis/client.py
    """

    def __init__(self, query):
        self.query = query

    def fetchall(self):
        return list(self.query.fetch_data())

    @property
    def columns(self):
        return [field.name for field in self.query.schema]

    def __enter__(self):
        # For compatibility when constructed from Query.execute()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass


class MapD(Query):

    def __init__(self, client, ddl, query_parameters=None):
        super(MapD, self).__init__(client, ddl)
        self.query_parameters = query_parameters or {}

    def _fetch(self, cursor):
        df = pd.DataFrame(cursor.fetchall(), columns=cursor.columns)
        return self.schema().apply_to(df)

    def execute(self):
        # synchronous by default
        with self.client._execute(
            self.compiled_ddl,
            results=True,
            query_parameters=self.query_parameters
        ) as cur:
            result = self._fetch(cur)

        return self._wrap_result(result)


class MapDAPIProxy(object):

    def __init__(self, project_id):
        self._client = pymapd.Client(project_id)

    @property
    def client(self):
        return self._client

    @property
    def project_id(self):
        return self.client.project

    def get_datasets(self):
        return list(self.client.list_datasets())

    def get_dataset(self, dataset_id):
        return self.client.dataset(dataset_id)

    def get_table(self, table_id, dataset_id, reload=True):
        (table_id, dataset_id) = _ensure_split(table_id, dataset_id)
        table = self.client.dataset(dataset_id).table(table_id)
        if reload:
            table.reload()
        return table

    def get_schema(self, table_id, dataset_id):
        return self.get_table(table_id, dataset_id).schema

    def run_sync_query(self, stmt):
        query = self.client.run_sync_query(stmt)
        query.use_legacy_sql = False
        query.run()
        # run_sync_query is not really synchronous: there's a timeout
        while not query.job.done():
            query.job.reload()
            time.sleep(0.1)
        return query


class MapDDatabase(Database):
    pass


pymapd_param = Dispatcher('pymapd_param')


@pymapd_param.register(ir.StructScalar, collections.OrderedDict)
def pymapd_param_struct(param, value):
    field_params = [pymapd_param(param[k], v) for k, v in value.items()]
    return pymapd.StructQueryParameter(param.get_name(), *field_params)


@pymapd_param.register(ir.ArrayValue, list)
def pymapd_param_array(param, value):
    param_type = param.type()
    assert isinstance(param_type, dt.Array), str(param_type)

    try:
        pymapd_type = _IBIS_TYPE_TO_DTYPE[str(param_type.value_type)]
    except KeyError:
        raise com.UnsupportedBackendType(param_type)
    else:
        return pymapd.ArrayQueryParameter(param.get_name(), pymapd_type, value)


@pymapd_param.register(
    ir.TimestampScalar,
    six.string_types + (datetime.datetime, datetime.date)
)
def pymapd_param_timestamp(param, value):
    assert isinstance(param.type(), dt.Timestamp)

    # TODO(phillipc): Not sure if this is the correct way to do this.
    timestamp_value = pd.Timestamp(value, tz='UTC').to_pydatetime()
    return pymapd.ScalarQueryParameter(
        param.get_name(), 'TIMESTAMP', timestamp_value)


@pymapd_param.register(ir.StringScalar, six.string_types)
def pymapd_param_string(param, value):
    return pymapd.ScalarQueryParameter(param.get_name(), 'STRING', value)


@pymapd_param.register(ir.IntegerScalar, six.integer_types)
def pymapd_param_integer(param, value):
    return pymapd.ScalarQueryParameter(param.get_name(), 'INT64', value)


@pymapd_param.register(ir.FloatingScalar, float)
def pymapd_param_double(param, value):
    return pymapd.ScalarQueryParameter(param.get_name(), 'FLOAT64', value)


@pymapd_param.register(ir.BooleanScalar, bool)
def pymapd_param_boolean(param, value):
    return pymapd.ScalarQueryParameter(param.get_name(), 'BOOL', value)


@pymapd_param.register(ir.DateScalar, six.string_types)
def pymapd_param_date_string(param, value):
    return pymapd_param(param, pd.Timestamp(value).to_pydatetime().date())


@pymapd_param.register(ir.DateScalar, datetime.datetime)
def pymapd_param_date_datetime(param, value):
    return pymapd_param(param, value.date())


@pymapd_param.register(ir.DateScalar, datetime.date)
def pymapd_param_date(param, value):
    return pymapd.ScalarQueryParameter(param.get_name(), 'DATE', value)


class MapDClient(SQLClient):

    sync_query = MapD
    database_class = MapDDatabase
    proxy_class = MapDAPIProxy
    dialect = comp.MapDDialect

    def __init__(self, *args, **kwargs):
        self.con = pymapd.connect(*args, **kwargs)

    @property
    def _table_expr_klass(self):
        return ir.TableExpr

    def table(self, *args, **kwargs):
        t = super(MapDClient, self).table(*args, **kwargs)
        if NATIVE_PARTITION_COL in t.columns:
            col = ibis.options.pymapd.partition_col
            assert col not in t
            return (
                t.mutate(**{col: t[NATIVE_PARTITION_COL]})
                    .drop([NATIVE_PARTITION_COL])
            )
        return t

    def _build_ast(self, expr, context):
        result = comp.build_ast(expr, context)
        return result

    def _execute_query(self, ddl, async=False):
        klass = self.async_query if async else self.sync_query
        inst = klass(self, ddl, query_parameters=ddl.context.params)
        return inst.execute()

    def _fully_qualified_name(self, name, database):
        dataset_id = database or self.dataset_id
        return dataset_id + '.' + name

    def _get_table_schema(self, qualified_name):
        return self.get_schema(qualified_name)

    def _execute(self, stmt, results=True, query_parameters=None):
        # TODO(phillipc): Allow **kwargs in calls to execute
        query = self._proxy.client.run_sync_query(stmt)
        query.use_legacy_sql = False
        query.query_parameters = [
            pymapd_param(param.to_expr(), value)
            for param, value in (query_parameters or {}).items()
        ]
        query.run()

        # run_sync_query is not really synchronous: there's a timeout
        while not query.job.done():
            query.job.reload()
            time.sleep(0.1)

        return MapDCursor(query)

    def database(self, name=None):
        if name is None:
            name = self.dataset_id
        return self.database_class(name, self)

    @property
    def current_database(self):
        return self.database(self.dataset_id)

    def set_database(self, name):
        self._dataset_id = name

    def exists_database(self, name):
        return self._proxy.get_dataset(name).exists()

    def list_databases(self, like=None):
        results = [dataset.name
                   for dataset in self._proxy.get_datasets()]
        if like:
            results = [
                dataset_name for dataset_name in results
                if re.match(like, dataset_name)
            ]
        return results

    def exists_table(self, name, database=None):
        (table_id, dataset_id) = _ensure_split(name, database)
        return self._proxy.get_table(table_id, dataset_id).exists()

    def list_tables(self, like=None, database=None):
        dataset = self._proxy.get_dataset(database or self.dataset_id)
        result = [table.name for table in dataset.list_tables()]
        if like:
            result = [
                table_name for table_name in result
                if re.match(like, table_name)
            ]
        return result

    def get_schema(self, name, database=None):
        (table_id, dataset_id) = _ensure_split(name, database)
        pymapd_table = self._proxy.get_table(table_id, dataset_id)
        return sch.infer(pymapd_table)

    @property
    def version(self):
        return parse_version(pymapd.__version__)


_DTYPE_TO_IBIS_TYPE = {
    'INT64': dt.int64,
    'FLOAT64': dt.double,
    'BOOL': dt.boolean,
    'STRING': dt.string,
    'DATE': dt.date,
    # FIXME: enforce no tz info
    'DATETIME': dt.timestamp,
    'TIME': dt.time,
    'TIMESTAMP': dt.timestamp,
    'BYTES': dt.binary,
}


_LEGACY_TO_STANDARD = {
    'INTEGER': 'INT64',
    'FLOAT': 'FLOAT64',
    'BOOLEAN': 'BOOL',
}


def _discover_type(field):
    typ = field.field_type
    if typ == 'RECORD':
        fields = field.fields
        assert fields
        names = [el.name for el in fields]
        ibis_types = [_discover_type(el) for el in fields]
        ibis_type = dt.Struct(names, ibis_types)
    else:
        ibis_type = _LEGACY_TO_STANDARD.get(typ, typ)
        ibis_type = _DTYPE_TO_IBIS_TYPE.get(ibis_type, ibis_type)
    if field.mode == 'REPEATED':
        ibis_type = dt.Array(ibis_type)
    return ibis_type


def pymapd_table_to_ibis_schema(table):
    pairs = [(el.name, _discover_type(el)) for el in table.schema]
    try:
        if table.list_partitions():
            pairs.append((NATIVE_PARTITION_COL, dt.timestamp))
    except Exception:
        pass
    return ibis.schema(pairs)
