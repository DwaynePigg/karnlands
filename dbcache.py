import sqlite3
import inspect
import itertools
import functools
import sys
import time
import types
import typing
from dataclasses import is_dataclass, astuple, fields
from datetime import timedelta
from pathlib import Path
from types import NoneType


# TODO: Annotated custom serializers
# TODO: forward reference types?
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
		self.signature = inspect.signature(func)
		self.table = func.__name__
		columns = []
		for name, param in self.signature.parameters.items():
			if param.annotation is inspect._empty:
				raise ValueError(f"type of parameter {name} must be given")
			columns.append(f"{name} {get_sql_type(param.annotation)}")
		try:
			return_type = func.__annotations__['return']
		except KeyError:
			raise ValueError('return type must be given')

		if return_type is tuple:
			raise ValueError('tuple return type must be parameterized')

		if is_dataclass(return_type):
			return_fields = fields(return_type)
			columns.extend(f"return${i} {get_sql_type(f.type)}" for i, f in enumerate(return_fields))
			return_columns = [f"return${i}" for i in range(len(return_fields))]
			self.result_concat = extend_dataclass
			self.get_return = lambda r: return_type(*r)
		elif typing.get_origin(return_type) is tuple:
			args = typing.get_args(return_type)
			columns.extend(f"return${i} {get_sql_type(a)}" for i, a in enumerate(args))
			return_columns = [f"return${i}" for i in range(len(args))]
			self.result_concat = list.extend
			self.get_return = lambda r: r
		else:
			columns.append(f"return {get_sql_type(return_type)}")
			return_columns = ['return']
			self.result_concat = list.append
			self.get_return = lambda r: r[0]
		
		if not return_columns:
			raise ValueError('return type of empty tuple/dataclass not allowed. Are you just testing me?')
	
		if timestamp:
			columns.append('timestamp INTEGER NOT NULL')
			return_columns.append('timestamp')

		self.conn.execute(f"CREATE TABLE IF NOT EXISTS {self.table} ({', '.join(columns)}, PRIMARY KEY({', '.join(self.signature.parameters.keys())})) WITHOUT ROWID")
		self.conn.commit()
		
		selectors = ' AND '.join(f'{name}=?' for name in self.signature.parameters.keys())
		self.lookup = f"SELECT {', '.join(return_columns)} FROM {self.table} WHERE {selectors}"
		insert_placeholders = ', '.join('?' for _ in range(len(self.signature.parameters) + len(return_columns)))
		insert_columns = ', '.join(itertools.chain(self.signature.parameters.keys(), return_columns))
		self.insert = f"INSERT OR REPLACE INTO {self.table} ({insert_columns}) VALUES ({insert_placeholders})"

	def get_values(self, args):
		bound = self.signature.bind(*args)
		bound.apply_defaults()
		return list(bound.arguments.values())
	
	def get_cached(self, values):
		try:
			return self.conn.execute(self.lookup, values).fetchone()
		except sqlite3.OperationalError as e:
			raise ValueError(f"{self.table} function signature has changed incompatibly") from e

	def clear(self):
		self.conn.execute(f"DELETE FROM {self.table}")
		self.conn.commit()
		self.size = 0

	def vacuum(self):
		self.conn.execute('VACUUM')
		self.conn.commit()


class SimpleCache(Cache):
	
	def __call__(self, *args, cache=True, cache_only=False):
		values = self.get_values(args)
		if cache:
			cached = self.get_cached(values)
			if cached is not None:
				return self.get_return(cached)
			if cache_only:
				raise CacheMiss(*args)

		result = self.func(*args)
		self.result_concat(values, result)
		self.conn.execute(self.insert, values)
		self.conn.commit()
		return result


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

	def evict(self, count):
		cur = self.conn.execute(f"DELETE FROM {self.table} WHERE timestamp <= (SELECT timestamp FROM {self.table} ORDER BY timestamp ASC LIMIT 1 OFFSET ?)", (count,))
		self.size -= cur.rowcount

	def __call__(self, *args, max_age=_UNSET, cache_only=False):
		values = self.get_values(args)
		cached = self.get_cached(values)
		if cached is not None:
			*return_values, timestamp = cached
			max_age = self.max_age if max_age is _UNSET else get_age(max_age)
			if time.time() - timestamp <= get_age(max_age):
				return self.get_return(return_values)
		
		if cache_only:
			raise CacheMiss(*args)

		result = self.func(*args)
		self.result_concat(values, result)
		values.append(int(time.time()))
		self.conn.execute(self.insert, values)
		if cached is None:
			self.size += 1
			if self.size > self.max_size:
				self.evict(self.evict_batch)

		self.conn.commit()
		return result


class CacheMiss(Exception):
	pass


SQLITE_TYPES = {
	bool: 'INTEGER',
	int: 'INTEGER',
	float: 'REAL',
	str: 'TEXT',
	bytes: 'BLOB',
	bytearray: 'BLOB',
}

def get_sql_type(tp: type) -> str:
	origin = typing.get_origin(tp)
	if origin is types.UnionType or origin is typing.Union:
		inner = unwrap_union(tp)
		nullable = True
	else:
		inner = tp
		nullable = False

	try:
		sql_type = SQLITE_TYPES[inner]
	except KeyError:
		raise ValueError(f"unsupported type: {tp}")
	return f"{sql_type} NOT NULL" if not nullable else sql_type


def unwrap_union(tp: type) -> type:
	args = typing.get_args(tp)
	if len(args) == 2:
		if args[1] is NoneType:
			return args[0]
		if args[0] is NoneType:
			return args[1]
	raise ValueError(f"Union not allowed except to express nullable type: {tp}")


def extend_dataclass(lst, dc):
	lst.extend(astuple(dc))


def get_age(age):
	if age is None:
		return sys.maxsize
	if isinstance(age, timedelta):
		return age.total_seconds()
	return age
