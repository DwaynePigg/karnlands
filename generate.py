import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

import ahocorasick
import requests
from jinja2 import Template

sys.path.append(str(Path(__file__).resolve().parents[1] / 'tcgplayer'))
from magicdatabase import DATABASE
from dbcache import database_cache
from magic import BASIC_LANDS

fix_quotes = str.maketrans("‘’“”", "''\"\"")

@dataclass
class Review:
	card: str
	rating: int
	review: list[str]
	
	@property
	def url(self):
		return scryfall_url(self.card)
	
	@property
	def img(self):
		return get_scryfall_image(self.card)


reviews_by_year = defaultdict(list)

with open('karnlands.txt', encoding='utf-8') as f:
	current_year = None
	current_card = None
	for line in f:
		line = line.strip().translate(fix_quotes)
		if not line:
			continue
			
		elif re.fullmatch(r'\d{4}', line):
			current_year = line.strip()
			current_card = None

		elif re.search(r'\s[*?]', line):
			*name_tokens, rating = line.split()
			current_card = Review(' '.join(name_tokens), len(rating) if rating != '?' else 0, [])
			reviews_by_year[current_year].append(current_card)
		
		else:
			if not current_card:
				current_card = Review('', 0, [])
				reviews_by_year[current_year].append(current_card)
			current_card.review.append(line)


def get_card(name):
	return DATABASE.cards_by_name[name.replace("The Urzatron", "Urza's Tower")][0]


@database_cache(file='scryfallimg.db')
def get_scryfall_image(card: str, size: str = 'normal') -> str:
	print(card)
	time.sleep(0.25)
	card = get_card(card)
	return requests.get(f"https://api.scryfall.com/cards/{card.code}/{card.cnum}").json()['image_uris'][size]


def load_linker():
	auto = ahocorasick.Automaton()
	ignore = {"Clear", "Sacrifice", "Sorry", *BASIC_LANDS}
	for printings in DATABASE.cards_by_name.values():
		card = printings[0]
		if card.name not in ignore:
			auto.add_word(card.name, card)
	
	def add_nickname(nickname, card_name):
		card = DATABASE.cards_by_name[card_name][0]
		auto.add_word(nickname, replace(card, name=nickname))
	
	add_nickname("Old Karn", "Karn, Silver Golem")
	add_nickname("New Karn", "Karn, Legacy Reforged")

	auto.make_automaton()
	return auto


CARD_LINKER = load_linker()


def scryfall_url(card):
	if isinstance(card, str):
		card = get_card(card)
	return f"https://scryfall.com/card/{card.code}/{card.cnum}"


def smart_quotes(s):
	def replace(match):
		c, q = match.groups()
		return c + ('‘’' if q == "'" else '“”')[bool(c)]
	return re.sub(r'([a-zA-Z0-9.,?!;:\'\"]?)([\'\"])', replace, s)


def link_cards(text):
	matches = []
	for end, card in CARD_LINKER.iter(text):
		start = end - len(card.name) + 1
		matches.append((start, end + 1, card))

	matches.sort(key=lambda m: (m[0], m[0] - m[1]))

	parts = []
	pos = 0
	for start, end, card in matches:
		if start < pos:
			continue
		parts.append(smart_quotes(text[pos:start]))
		parts.append(f'<a href="{scryfall_url(card)}">{card.name}</a>')
		pos = end

	parts.append(smart_quotes(text[pos:]))
	return ''.join(parts)


with open('template.html.jinja') as f:
    template = Template(f.read())

with open('index.html', 'w', encoding='utf-8') as f:
	f.write(template.render(
		reviews_by_year=reviews_by_year, 
		star='★',
		link_cards=link_cards,
	))
