from .parser import make_options_object
from ..core import pgnChecker as checker

import argparse


o = make_options_object()
print(o)
c = checker.PgnChecker(o)
c.run()