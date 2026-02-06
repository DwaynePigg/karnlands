import sqlite3
import inspect
import functools
from pathlib import Path


SQLITE_TYPES = { int: 'INTEGER', float: 'REAL', str: 'TEXT', bytes: 'BLOB' }
db_path=Path('.func.db')


def dbcache(func):
	sig = inspect.signature(func)
	table = func.__name__
	columns = []
	for name, param in sig.parameters.items():
		if param.annotation is inspect._empty:
			raise ValueError(f"type of parameter {name} must be given")
		columns.append(f"{name} {SQLITE_TYPES.get(param.annotation, 'BLOB')} NOT NULL")
	try:
		return_type = func.__annotations__['return']
	except KeyError:
		raise ValueError('return type must be given')
	columns.append(f'return {SQLITE_TYPES.get(return_type, 'BLOB')} NOT NULL')
	conn = sqlite3.connect(Path(db_path))
	conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)}, PRIMARY KEY({', '.join(sig.parameters.keys())}))")
	conn.commit()

	@functools.wraps(func)
	def wrapper(*args):
		bound = sig.bind(*args)
		bound.apply_defaults()
		values = list(bound.arguments.values())
		cur = conn.execute(f"SELECT return FROM {table} WHERE {' AND '.join(f'{name}=?' for name in bound.arguments.keys())}", values)
		row = cur.fetchone()
		if row:
			return row[0]

		result = func(*args)
		conn.execute(
			f"INSERT OR REPLACE INTO {table} ({', '.join(bound.arguments.keys())}, return) VALUES ({'?, ' * len(bound.arguments)}?)",
			[*values, result],
		)
		conn.commit()
		return result

	return wrapper

