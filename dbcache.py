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
try:
	from types import NoneType
except ImportError:
	NoneType = type(None)


# TODO: Annotated custom serializers
# TODO: forward reference types?
# TODO: growth_factor

def database_cache(file, max_age=None, max_size=None):
	advanced = max_age is not None or max_size is not None

	if advanced:
		max_age = _get_age(max_age)
		max_size = max_size or sys.maxsize
		if max_age < 1:
			raise ValueError(f"{max_age=}")

	def decorator(func):
		sig = inspect.signature(func)
		table = func.__name__
		columns = []
		for name, param in sig.parameters.items():
			if param.annotation is inspect._empty:
				raise ValueError(f"type of parameter {name} must be given")
			columns.append(f"{name} {_get_type(param.annotation)}")
		try:
			return_type = func.__annotations__['return']
		except KeyError:
			raise ValueError('return type must be given')

		if return_type is tuple:
			raise ValueError('tuple return type must be parameterized')
		if is_dataclass(return_type):
			return_fields = fields(return_type)
			columns.extend(f"return${i} {_get_type(f.type)}" for i, f in enumerate(return_fields))
			return_columns = [f"return${i}" for i in range(len(return_fields))]
			result_concat = _extend_dataclass
			get_return = lambda r: return_type(*r)
		elif typing.get_origin(return_type) is tuple:
			args = typing.get_args(return_type)
			columns.extend(f"return${i} {_get_type(a)}" for i, a in enumerate(args))
			return_columns = [f"return${i}" for i in range(len(args))]
			result_concat = list.extend
			get_return = lambda r: r
		else:
			columns.append(f"return {_get_type(return_type)}")
			return_columns = ['return']
			result_concat = list.append
			get_return = lambda r: r[0]
		
		if not return_columns:
			raise ValueError('return type of empty tuple/dataclass not allowed. Are you just testing me?')

		if advanced:
			columns.append('timestamp INTEGER NOT NULL')
			return_columns.append('timestamp')

		conn = sqlite3.connect(file)
		# TODO: question marks
		conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)}, PRIMARY KEY({', '.join(sig.parameters.keys())}))")
		conn.commit()
		
		selectors = ' AND '.join(f'{name}=?' for name in sig.parameters.keys())
		lookup = f"SELECT {', '.join(return_columns)} FROM {table} WHERE {selectors}"

		insert_placeholders = ', '.join('?' for _ in range(len(sig.parameters) + len(return_columns)))
		insert_columns = ', '.join(itertools.chain(sig.parameters.keys(), return_columns))
		insert = f"INSERT OR REPLACE INTO {table} ({insert_columns}) VALUES ({insert_placeholders})"
		
		def get_values(args):
			bound = sig.bind(*args)
			bound.apply_defaults()
			return list(bound.arguments.values())
		
		def get_cached(values):
			try:
				return conn.execute(lookup, values).fetchone()
			except sqlite3.OperationalError as e:
				raise ValueError(f"{table} function signature has changed incompatibly") from e

		if advanced:
			size = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
			def evict(n):
				conn.execute(f"DELETE FROM {table} WHERE rowid = (SELECT rowid FROM {table} ORDER BY timestamp ASC LIMIT ?)", (n,))
			if size > max_size:
				evict(size - max_size)
			
			@functools.wraps(func)
			def wrapper(*args, max_age=max_age, cache_only=False):
				nonlocal size
				values = get_values(args)
				cached = get_cached(values)
				if cached is not None:
					*return_values, timestamp = cached
					if time.time() - timestamp <= _get_age(max_age):
						return get_return(return_values)
				
				if cache_only:
					raise CacheMiss(*args)

				result = func(*args)
				result_concat(values, result)
				values.append(int(time.time()))
				conn.execute(insert, values)
				if cached is None:
					size += 1
					if size > max_size:
						evict(1)
						size -= 1

				conn.commit()
				return result
		
		else:

			@functools.wraps(func)
			def wrapper(*args, cache=1):
				values = get_values(args)
				if cache:
					cached = get_cached(values)
					if cached is not None:
						return get_return(cached)
					if cache == 2:
						raise CacheMiss(*args)

				result = func(*args)
				result_concat(values, result)
				conn.execute(insert, values)
				conn.commit()
				return result

		return wrapper

	return decorator


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

def _get_type(tp: type) -> str:
	origin = typing.get_origin(tp)
	if origin is types.UnionType or origin is typing.Union:
		inner = _get_union(tp)
		nullable = True
	else:
		inner = tp
		nullable = False

	try:
		sql_type = SQLITE_TYPES[inner]
	except KeyError:
		raise ValueError(f"unsupported type: {tp}")
	return f"{sql_type} NOT NULL" if not nullable else sql_type


def _get_union(tp: type) -> type:
	args = typing.get_args(tp)
	if len(args) == 2:
		if args[1] is NoneType:
			return args[0]
		if args[0] is NoneType:
			return args[1]
	raise ValueError(f"Union not allowed except to express nullable type: {tp}")


def _extend_dataclass(lst, dc):
	lst.extend(astuple(dc))


def _get_age(age):
	if age is None:
		return sys.maxsize
	if isinstance(age, timedelta):
		return age.total_seconds()
	return age
