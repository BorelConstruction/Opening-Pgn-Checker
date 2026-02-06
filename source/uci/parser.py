from ..core.options import Options, load_settings, save_settings
from ..core import pgnChecker as checker
import argparse


from dataclasses import fields, MISSING

def add_options_to_parser(parser: argparse.ArgumentParser, options_class):
    """
    Dynamically adds arguments to an ArgumentParser based on 
    dataclass fields and their metadata.
    """
    for f in fields(options_class):
        # Extract metadata
        meta = f.metadata
        label = meta.get("label", f.name)
        help_text = meta.get("help", label)
        
        arg_name = f"--{f.name.replace('_', '-')}"

        # Build argument kwargs
        kwargs = {
            "help": help_text,
            "default": f.default if f.default is not MISSING else None,
            "type": f.type,
        }
        # Handle numeric constraints (UCI 'spin' type)
        if "min" in meta and "max" in meta:
            kwargs["metavar"] = f"[{meta['min']}-{meta['max']}]"
            # Note: You could use a custom 'range' type or choices if preferred

        # Handle booleans (UCI 'check' type)
        if f.type is bool:
            kwargs.pop("type")  # arg_parse handles bools via actions
            kwargs["action"] = "store_true" if not f.default else "store_false"

        # want pgn to be a positional arg; this and its dest is then determined by arg_name
        # then (no -- and _). This is somewhat ad hoc but I don't care about CLI
        if f.name == 'input_pgn':
            arg_name = 'input_pgn'
        else:
            kwargs["dest"] = f.name

        parser.add_argument(arg_name, **kwargs)

    return parser

def make_parser():
    parser = argparse.ArgumentParser(
            usage = 'usage: pgnChecker [options] FILE.pgn',
            description = '''Checks if any popular moves have been missed in
            an opening theory file.''',
            epilog='''Quick instructions:
            (1) Prepare lines or games to analyze in FILE.pgn.
            (2) ???
            (3) PROFIT''')
    add_options_to_parser(parser, Options)
    return parser

def make_options_object():
    parser = make_parser()
    args = parser.parse_args()
    print(args.min_depth)
    return Options(**vars(args))

if __name__=='__main__':
    pass