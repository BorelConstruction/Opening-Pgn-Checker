import sys
import time
import berserk

from chess import Color as Color
from chess import WHITE
from typing import Union

from .options import DEFAULT_DB_RATINGS

DEFAULT_DB_SPEEDS = ["blitz", "rapid", "classical"]


def safe_get_games(
    opening_explorer: berserk.OpeningStatistic,
    *args,
    max_attempts=5,
    lichess=True,
    base_delay=30.0,
    ratings: list[str] | None = None,
    speeds: list[str] | None = None,
    **kwargs,
) -> dict:
    '''Query the database, retrying if HTTP 429 is raised
        (which means we query too often)'''
    if lichess:
        lichess_ratings = ratings if ratings is not None else DEFAULT_DB_RATINGS
        lichess_speeds = speeds if speeds is not None else DEFAULT_DB_SPEEDS
    
    time.sleep(0.1)
    for attempt in range(max_attempts):
        try:
            sys.stderr.write("\n querying the DB...")
            if lichess:
                games = opening_explorer.get_lichess_games(
                    *args,
                    **kwargs,
                    ratings=lichess_ratings,
                    speeds=lichess_speeds,
                )
            else:
                games = opening_explorer.get_masters_games(*args, **kwargs)
            return games

        except berserk.exceptions.ResponseError as e: # TODO: ApiError
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                # exponential backoff
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                sys.stderr.write(f"\n 429, {attempt}")
            elif status in (500, 502, 503, 504): # not an expert, not sure what time to wait for max robustness
                time.sleep(10)
                continue
            elif status in (401, 403):
                url = None
                try:
                    url = e.response.request.url
                except AttributeError:
                    pass

                url_part = f" ({url})" if url else ""
                raise RuntimeError(
                    "Lichess opening explorer request was rejected "
                    f"with HTTP {status}{url_part}. "
                    "\nPerhaps an invalid/empty API token was sent or "
                    "you are behind a proxy/VPN/captive portal. "
                ) from e
            else:
                raise
        except berserk.exceptions.ApiError as e:
            sys.stderr.write(f"\n {e}")
            if attempt < max_attempts - 1:
                sys.stderr.write(f"\nRetrying...")
                time.sleep(10)
            continue

    raise RuntimeError("Too many failed attempts – giving up")

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
