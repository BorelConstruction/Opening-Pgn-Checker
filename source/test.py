from optparse import OptionParser
from pathlib import Path
from typing import Iterable
import berserk
import berserk.clients.opening_explorer
import berserk.exceptions
import chess
import chess.engine
import chess.pgn
import chess.svg
import json
import logging
import os
import sys
import traceback
import time
from requests.exceptions import HTTPError
from typing import Union, TextIO
import tempfile

# os.getcwd()

def checkpoint_pgn(game, output_path: str):
    dir_ = os.path.dirname(output_path)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=dir_,
        delete=False
    ) as tmp:
        tmp.write(str(game))   # or game.accept(exporter)
        tmp.flush()
        os.fsync(tmp.fileno())

    os.replace(tmp.name, output_path)


def write_rs(game : list):
    game.append('r')

def test_saving():
    game = []
    for i in range(20):
        write_rs(game)
        sys.stderr.write(f"i = {i}, going to sleep... \n")
        time.sleep(1)
        if i % 3 == 0:
            checkpoint_pgn(game, 'test_output.txt')
            sys.stderr.write(f'saving, should be {i + 1} rs \n')

def test(options):
    with open(options.input_pgn, encoding="utf-8") as pgnFile:
        log_node = chess.pgn.read_game(pgnFile)
        print(count_nodes(log_node))