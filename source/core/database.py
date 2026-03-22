import sys
import time
import berserk

from chess import Color as Color
from chess import WHITE
from typing import Union


ratings = ["2200", "2500"] # TODO: make parameters
speeds = ["blitz", "rapid", "classical"]

ratings_n = ["1900", "2200"]
speeds_n = ["blitz", "rapid", "classical"]

def safe_get_games(opening_explorer: berserk.OpeningStatistic, *args, max_attempts=5, lichess=True, base_delay=30.0, **kwargs) -> dict:
    '''Query the database, retrying if HTTP 429 is raised
        (which means we query too often)'''
    time.sleep(0.1)
    for attempt in range(max_attempts):
        try:
            sys.stderr.write("\n querying the DB...")
            if lichess:
                games = opening_explorer.get_lichess_games(*args, **kwargs, ratings=ratings, speeds=speeds)
            else:
                games = opening_explorer.get_masters_games(*args, **kwargs)
            return games

        except berserk.exceptions.ResponseError as e:
            if e.response is not None and e.response.status_code == 429:
                # exponential backoff
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                sys.stderr.write(f"\n 429, {attempt}")
            else:
                raise
        except Exception as e:
            raise  # not a 429 → bubble up

    raise RuntimeError("Too many 429s – giving up")

def total_games(game_data: dict):
    return game_data['white'] + game_data['draws'] + game_data['black']

def total_decisive_games(game_data: dict):
    return game_data['white'] + game_data['black']

def score_rate(game_data: dict, side: Union[str, Color]):
    if isinstance(side, Color):
        side = 'white' if side == WHITE else 'black'
    return (game_data[side] + 0.5 * game_data['draws']) / total_games(game_data)

def win_rate(game_data: dict, side: Union[str, Color]):
    if isinstance(side, Color):
        side = 'white' if side == WHITE else 'black'
    return game_data[side]/total_decisive_games(game_data)

def move_frequency(move_data: dict, games: dict):
    return total_games(move_data)/total_games(games)

def move_freq_frac(move_data: dict, games: dict):
    return total_games(move_data), total_games(games)