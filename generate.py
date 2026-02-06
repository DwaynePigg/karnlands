import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import ahocorasick
import requests
from jinja2 import Template

sys.path.append(str(Path(__file__).resolve().parents[1] / 'tcgplayer'))
from magicdatabase import DATABASE
from dbcache import database_cache


@dataclass
class Review:
	card: str
	rating: int
	review: list[str]


reviews_by_year = defaultdict(list)

with open('karnlands.txt', encoding='utf-8') as f:
	current_year = None
	current_card = None
	for line in f:
		line = line.strip()
		if not line:
			continue
			
		elif re.match(r'\d{4}', line):
			current_year = line.strip()
		
		elif re.search(r': See', line):
			card, see = line.split(': See')
			reviews_by_year[current_year].append(Review(card, 0, [f"See: {see}"]))

		elif re.search(r'\s[*?]', line):
			*name_tokens, rating = line.split()
			current_card = Review(' '.join(name_tokens), len(rating) if rating != '?' else 0, [])
			reviews_by_year[current_year].append(current_card)
		
		else:
			current_card.review.append(line)


@database_cache
def get_scryfall_image(card: str, size: str = 'small') -> str:
	print(card)
	time.sleep(0.25)
	card = DATABASE.cards_by_name[card][0]
	return requests.get(f"https://api.scryfall.com/cards/{card.code}/{card.cnum}").json()['image_uris'][size]


auto = ahocorasick.Automaton()
for printings in DATABASE.cards_by_name.values():
	card = printings[0]
	auto.add_word(card.name, card)

auto.make_automaton()


def scryfall_url(card):
	if isinstance(card, str):
		card = DATABASE.cards_by_name[card][0]
	return f"https://scryfall.com/card/{card.code}/{card.cnum}"


def link_cards(text):
	matches = []
	for end, card in auto.iter(text):
		start = end - len(card.name) + 1
		matches.append((start, end + 1, card))

	matches.sort(key=lambda m: (m[0], -(m[1] - m[0])))
	
	parts = []
	pos = 0
	for start, end, card in matches:
		if start < pos:
			continue
		parts.append(text[pos:start])
		parts.append(f'<a href="{scryfall_url(card)}">{card.name}</a>')
		pos = end

	parts.append(text[pos:])
	return ''.join(parts)


with open('template.html.jinja') as f:
    template = Template(f.read())

with open('index.html', 'w', encoding='utf-8') as f:
	f.write(template.render(
		reviews_by_year=reviews_by_year, 
		star='★',
		image=get_scryfall_image,
		scryfall_url=scryfall_url,
		link_cards=link_cards,
	))
