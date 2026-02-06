import sqlite3
import inspect
import itertools
import functools
import types
import typing
from pathlib import Path
try:
	from types import NoneType
except ImportError:
	NoneType = type(None)


SQLITE_TYPES = { int: 'INTEGER', float: 'REAL', str: 'TEXT', bytes: 'BLOB', bool: 'INTEGER' }
db_path=Path('.func.db')


def get_sql_type(tp: type) -> str:
	origin = typing.get_origin(tp)
	if origin is types.UnionType or origin is typing.Union:
		args = typing.get_args(tp)
		if len(args) != 2:
			raise ValueError(f"unsupported union: {tp}")
		if args[1] is NoneType:
			inner = args[0]
		elif args[0] is NoneType:
			inner = args[1]
		else:
			raise ValueError(f"unsupported union: {tp}")
		nullable = True
	else:
		inner = tp
		nullable = False

	try:
		sql_type = SQLITE_TYPES[inner]
	except KeyError:
		raise ValueError(f"unsupported type: {tp}")
	return f"{sql_type} NOT NULL" if not nullable else sql_type


def database_cache(func):
	sig = inspect.signature(func)
	table = func.__name__
	columns = []
	for name, param in sig.parameters.items():
		if param.annotation is inspect._empty:
			raise ValueError(f"type of parameter {name} must be given")
		columns.append(f"{name} {get_sql_type(param.annotation)}")
	try:
		return_type = func.__annotations__['return']
	except KeyError:
		raise ValueError('return type must be given')

	origin = typing.get_origin(return_type)
	# TODO: handle optional tuple?
	if origin is None or origin is not tuple:
		columns.append(f"return {get_sql_type(return_type)}")
		return_columns = ['return']
		result_concat = list.append
		unwrap_row = lambda r: r[0]
	else:
		args = typing.get_args(return_type)
		if not args:
			raise ValueError('tuple must be typed')
		columns.extend(f"return_{i} {get_sql_type(a)}" for i, a in enumerate(args))
		return_columns = [f"return_{i}" for i in range(len(args))]
		result_concat = list.extend
		unwrap_row = lambda r: r

	conn = sqlite3.connect(db_path)
	conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)}, PRIMARY KEY({', '.join(sig.parameters.keys())}))")
	conn.commit()
	
	selectors = ' AND '.join(f'{name}=?' for name in sig.parameters.keys())
	lookup = f"SELECT {', '.join(return_columns)} FROM {table} WHERE {selectors}"

	insert_placeholders = ', '.join('?' for _ in range(len(sig.parameters) + len(return_columns)))
	insert_columns = ', '.join(itertools.chain(sig.parameters.keys(), return_columns))
	insert = f"INSERT OR REPLACE INTO {table} ({insert_columns}) VALUES ({insert_placeholders})"

	@functools.wraps(func)
	def wrapper(*args):
		bound = sig.bind(*args)
		bound.apply_defaults()
		values = list(bound.arguments.values())
		cur = conn.execute(lookup, values)
		row = cur.fetchone()
		if row:
			return unwrap_row(row)

		result = func(*args)
		result_concat(values, result)
		conn.execute(insert, values)
		conn.commit()
		return result

	return wrapper
