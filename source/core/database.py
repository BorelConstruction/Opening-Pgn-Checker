import sys
import time
import berserk


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