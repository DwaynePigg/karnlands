import functools
import inspect
import itertools
import sqlite3
import sys
import time
import types
import typing
from dataclasses import is_dataclass, astuple, fields
from datetime import timedelta
from pathlib import Path
from types import NoneType


_UNSET = object()


def database_cache(file, max_age=None, max_size=None, evict_batch=None):
	if max_age is None and max_size is None:
		return lambda func: SimpleCache(func, file)
	return lambda func: TimestampCache(func, file, max_age, max_size, evict_batch)


class Cache:

	def __init__(self, func, file, *, timestamp=False):
		functools.update_wrapper(self, func)
		self.conn = sqlite3.connect(file)
		self.func = func
		self.table = func.__name__
		self.signature = inspect.signature(func)
		
		param_columns = []
		
		for name, param in self.signature.parameters.items():
			if param.annotation is inspect._empty:
				raise ValueError(f"type of parameter {name} must be given")
			param_columns.append(Column(name, param.annotation))

		try:
			return_type = func.__annotations__['return']
		except KeyError:
			raise ValueError('return type must be given')

		if return_type is tuple:
			raise ValueError('tuple return type must be parameterized')

		if is_dataclass(return_type):
			self.return_handler = DataclassReturn([Column(f"return${i}", f.type) for i, f in enumerate(fields(return_type))], return_type)
		elif typing.get_origin(return_type) is tuple:
			self.return_handler = TupleReturn([Column(f"return${i}", a) for i, a in enumerate(typing.get_args(return_type))])
		else:
			self.return_handler = SingleReturn(Column(f"return", return_type))

		self.columns = [*param_columns, *self.return_handler.columns]
		
		if timestamp:
			self.columns.append(Column('timestamp', int))

		self.conn.execute(f"""
			CREATE TABLE IF NOT EXISTS {self.table} (
				{', '.join(col.col_def for col in self.columns)}, 
				PRIMARY KEY({', '.join(col.name for col in param_columns)})
			) WITHOUT ROWID""")
		self.conn.commit()
	
		self.lookup_cmd = f"SELECT {', '.join(col.name for col in self.return_handler.columns)} FROM {self.table} WHERE {' AND '.join(f'{col.name}=?' for col in param_columns)}"
		self.insert_cmd = f"INSERT OR REPLACE INTO {self.table} ({', '.join(col.name for col in self.columns)}) VALUES ({', '.join('?' for _ in self.columns)})"

	def get_values(self, args):
		bound = self.signature.bind(*args)
		bound.apply_defaults()
		return [col.serialize(val) for col, val in zip(self.columns, bound.arguments.values())]
	
	def get_cached(self, values):
		try:
			return self.conn.execute(self.lookup_cmd, values).fetchone()
		except sqlite3.OperationalError as e:
			raise ValueError(f"{self.table} function signature has changed incompatibly") from e
		
	def clear(self):
		self.conn.execute(f"DELETE FROM {self.table}")
		self.conn.commit()

	def vacuum(self):
		self.conn.execute('VACUUM')
		self.conn.commit()

	def all_rows(self):
		return self.conn.execute(f"SELECT {', '.join(col.name for col in self.columns)} FROM {self.table}").fetchall()


class SimpleCache(Cache):
	
	def __call__(self, *args, cache=True, cache_only=False):
		values = self.get_values(args)
		if cache:
			cached = self.get_cached(values)
			if cached is not None:
				return self.return_handler.get(cached)
			if cache_only:
				raise CacheMiss(*args)

		result = self.func(*args)
		self.return_handler.concat(values, result)
		self.conn.execute(self.insert_cmd, values)
		self.conn.commit()
		return result

	def contents(self):
		arg_len = len(self.signature.parameters)
		for row in self.all_rows():
			params = tuple(col.deserialize(val) for col, val in zip(self.columns, row[:arg_len]))
			yield params, self.return_handler.get(row[arg_len:])


class TimestampCache(Cache):

	def __init__(self, func, file, max_age=None, max_size=None, evict_batch=None):
		super().__init__(func, file, timestamp=True)
		self.max_age = get_age(max_age)
		if self.max_age < 1:
			raise ValueError(f"{max_age=}")

		self.size = self.conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]
		if not max_size:
			self.max_size = sys.maxsize
			self.evict_batch = None
		else:
			self.max_size = max_size
			self.evict_batch = evict_batch or int(0.2 * max_size)
			# if max_size changed between runs, reduce the size now
			if self.size > max_size:
				self.evict(self.size - max_size)
				self.vacuum()
		
		self.evict_cmd = f"DELETE FROM {self.table} WHERE timestamp <= (SELECT timestamp FROM {self.table} ORDER BY timestamp ASC LIMIT 1 OFFSET ?)"

	def evict(self, count):
		cur = self.conn.execute(self.evict_cmd, (count,))
		self.size -= cur.rowcount

	def __call__(self, *args, max_age=_UNSET, cache_only=False):
		values = self.get_values(args)
		cached = self.get_cached(values)
		if cached is not None:
			*return_values, timestamp = cached
			max_age = self.max_age if max_age is _UNSET else get_age(max_age)
			if time.time() - timestamp <= get_age(max_age):
				return self.return_handler.get(return_values)
		
		if cache_only:
			raise CacheMiss(*args)

		result = self.func(*args)
		self.return_handler.concat(values, result)
		values.append(int(time.time()))
		self.conn.execute(self.insert_cmd, values)
		if cached is None:
			self.size += 1
			if self.size > self.max_size:
				self.evict(self.evict_batch)

		self.conn.commit()
		return result

	def clear(self):
		super().clear()
		self.size = 0

	def all_rows(self):
		return self.conn.execute(f"SELECT {', '.join(col.name for col in self.columns)} FROM {self.table} ORDER BY timestamp ASC").fetchall()

	def contents(self):
		arg_len = len(self.signature.parameters)
		for row in self.all_rows():
			params = tuple(col.deserialize(val) for col, val in zip(self.columns, row[:arg_len]))
			yield params, self.get_return(row[arg_len:-1]), row[-1]


class CacheMiss(Exception):
	pass


def identity(x):
	return x


SQLITE_TYPES = {
	bool: 'INTEGER',
	int: 'INTEGER',
	float: 'REAL',
	str: 'TEXT',
	bytes: 'BLOB',
	bytearray: 'BLOB',
}


class Column:
	
	def __init__(self, name: str, tp: type):
		self.name = name
		if typing.get_origin(tp) is typing.Annotated:
			unused, (self.serialize , self.deserialize) = typing.get_args(tp)
			try:
				tp = self.serialize.__annotations__['return']
			except KeyError:
				raise ValueError('serializer function must have return type')
		else:
			self.serialize = identity
			self.deserialize = identity

		origin = typing.get_origin(tp)
		if origin is types.UnionType or origin is typing.Union:
			self.base_type = unwrap_union(tp)
			self.nullable = True
		else:
			self.base_type = tp
			self.nullable = False
		
		try:
			self.sql_type = SQLITE_TYPES[self.base_type]
		except KeyError:
			raise ValueError(f"unsupported type: {tp}")

	@property
	def col_def(self):
		return f"{self.name} {self.sql_type}{'' if self.nullable else ' NOT NULL'}"
	
	def __repr__(self):
		return f"({self.col_def}: >>{self.serialize.__name__}, <<{self.deserialize.__name__})"


def unwrap_union(tp: type) -> type:
	args = typing.get_args(tp)
	if len(args) == 2:
		if args[1] is NoneType:
			return args[0]
		if args[0] is NoneType:
			return args[1]
	raise ValueError(f"Union not allowed except to express nullable type: {tp}")


def get_age(age):
	if age is None:
		return sys.maxsize
	if isinstance(age, timedelta):
		return age.total_seconds()
	return age


class ReturnHandler:

	def concat(self, row, result):
		raise NotImplementedError
	
	def get(self, values):
		raise NotImplementedError


class SingleReturn(ReturnHandler):

	def __init__(self, column):
		self.column = column

	def concat(self, row, result):
		row.append(self.column.serialize(result))
	
	def get(self, values):
		return self.column.deserialize(values[0])

	@property
	def columns(self):
		return [self.column]


class TupleReturn(ReturnHandler):

	def __init__(self, columns):
		self.columns = columns

	def concat(self, row, result):
		row.extend(col.serialize(val) for col, val in zip(self.columns, result))
	
	def get(self, values):
		return tuple(col.deserialize(val) for col, val in zip(self.columns, values))


class DataclassReturn(ReturnHandler):

	def __init__(self, columns, dataclass_type):
		self.columns = columns
		self.dataclass_type = dataclass_type
	
	def concat(self, row, result):
		row.extend(col.serialize(val) for col, val in zip(self.columns, astuple(result)))
	
	def get(self, values):
		return self.dataclass_type(*(col.deserialize(val) for col, val in zip(self.columns, values)))
