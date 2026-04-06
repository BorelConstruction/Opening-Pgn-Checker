from dataclasses import dataclass, field, asdict, fields
import json
import os

from hashlib import sha1


CONFIG_FILE = "settings.json"

DEBUG_MODE = False

'''
    CoreOptions are the options that we expect to stay constant throughout the program.
    Note that this is is not the same as common options. For example,
    two features may both use input_pgn/starting positions but different ones,
    and we want to keep them separate.
    # TODO: just make each feature remember its own options? Then "feature 1. start_pos != feature 2. start_pos",
    # and by default we can populate from other feature's options if they exist.
'''

@dataclass
class CoreOptions:
    # --- PROVIDED ONCE AND FOR ALL ---
    engine_path: str = field(
        default="",
        metadata={"label": "Engine Path", "ui_hint": "file_path",
            "file_filter": "EXE files (*.exe)",}
    )

    _token: str = field(
        default="",
        metadata={"label": "Lichess API Token", "ui_hint": "password"}
    )

    # --- ENGINE SETTINGS ---
    min_depth: int = field(
        default=28,
        metadata={"label": "Minimum Engine Depth (ply)", "min": 1, "max": 60, 
                  "ui_group": "Engine Settings", "ui_group_order": 1}
    )
    max_depth: int = field(
        default=40,
        metadata={"label": "Maximum Engine Depth (ply)", "min": 1, "max": 80,
                   "ui_group": "Engine Settings", "ui_group_order": 2}
    )

    # --- TRAVERSAL SETTINGS ---
    check_alternatives: bool = field(
        default=False,
        metadata={"label": "Check Our Alternatives"}
    )

    # --- DATABASE SETTINGS ---
    db_types: list = field(
        default_factory=lambda: ["db_lichess"],
        metadata={"label": "Database Types", "options": {"Lichess": "db_lichess", "Masters": "db_masters"}}
    )


    def validate(self):
        if self.min_depth > self.max_depth:
            raise ValueError("min_depth must be ≤ max_depth")
        

@dataclass
class RepertoireOptions(CoreOptions):
    """
    Options shared by features that operate on a repertoire PGN.
    """

    input_pgn: str = field(
        default="",
        metadata={
            "label": "Input PGN",
            "ui_hint": "file_path",
            "file_filter": "PGN files (*.pgn)",
            "initial_dir": "input pgns",
        },
    )

    play_white: bool = field(
        default=True,
        metadata={"label": "Play as White"},
    )

    start_move: int = field(
        default=6,
        metadata={
            "label": "Start on Move",
            "min": 2,
            "max": 60,
            "ui_group": "analysis_move_range",
            "ui_group_order": 1,
        },
    )

    end_move: int = field(
        default=20,
        metadata={
            "label": "End on Move",
            "min": 2,
            "max": 80,
            "ui_group": "analysis_move_range",
            "ui_group_order": 2,
        },
    )

    def validate(self):
        super().validate()
        if not self.input_pgn:
            raise ValueError("No opening PGN selected")
        if self.start_move > self.end_move:
            raise ValueError("start_move must be ≤ end_move")


@dataclass
class CheckerOptions(RepertoireOptions):
    # --- WHAT TO WORK WITH ---
    starting_pos: str = field(
        default="",
        metadata={"label": "Starting Position (FEN)"}
    )

    # --- WHAT TO DO WITH IT ---
    actions: list = field(
        default_factory=lambda: ["find_gaps"],
        metadata={"label": "Actions", "options": {"Find Gaps": "find_gaps", "Fill Gaps": "fill_gaps",
                                                   "Mark Moves": "mark_moves", "Seek Consistency": "seek_consistency"}}
    )

    # --- HOW TO WORK WITH IT ---
    # --- MOVE CHOICE ---
    freq_threshold: float = field(
        default=0.15,
        metadata={"label": "Frequency Threshold", "ui_hint": "percentage", "min": 0.0, "max": 1.0, "step": 0.025,
                  "ui_group": "move_choice", "ui_group_order": 1,}
    )
    min_games: int = field(  # book cutoff
        default=10,
        metadata={"label": "Minimum Number of Games", "min": 1, "max": 500,
                  "ui_group": "move_choice", "ui_group_order": 2,}
    )

    added_depth: int = field(
        default=3,
        metadata={"label": "Length of Suggested Lines", "min": 1, "max": 20}
    )

    use_engine_for_them: bool = field(
        default=False,
        metadata={"label": "Engine for Opponent's Move"}
    )

    # --- DETAILS ON WHAT TO DO ---
    add_nag: bool = field(
        default=True,
        metadata={"label": "Add NAG Annotations (+-, !?, etc.)"}
    )
    trim_obvious_moves: bool = field(
        default=True,
        metadata={"label": "Trim Obvious Moves"}
    )

    # --- OUTPUT ---  (currently don't use this)
    output_pgn: str = field(
        default="Output.pgn",
        metadata={"label": "Output PGN Filename", "ui_hint": "save_file"}
    )

    def validate(self):
        super().validate()
        if "fill_gaps" in self.actions and "find_gaps" not in self.actions:
            raise ValueError("Fill Gaps action requires Find Gaps to be selected")


@dataclass
class SpacedRepetitionOptions(RepertoireOptions):
    non_file_move_frequency: float = field(
        default=0.20,
        metadata={
            "label": "Non-file Move Frequency",
            "ui_hint": "percentage",
            "min": 0.0,
            "max": 1.0,
            "step": 0.025,
        },
    )

    def validate(self):
        super().validate()
        if not (0.0 <= self.non_file_move_frequency <= 1.0):
            raise ValueError("non_file_move_frequency must be between 0 and 1")

@dataclass
class GraphOptions(CoreOptions):
    input_pgn: str = field(
        default='',
        metadata={
            "label": "Input PGN",
            "ui_hint": "file_path",
            "file_filter": "PGN files (*.pgn)",
            "initial_dir": "input pgns"
        }
    )
    
    starting_pos: str = field(
        default="",
        metadata={"label": "Starting Position (FEN)"}
    )
    # def __post_init__(self):
    #     self.validate()

    depth : int = field(
        default=10,
        metadata={"label": "Depth", "min": 1, "max": 80}
    )

    freq_threshold: float = field(
        default=0.20,
        metadata={"label": "Frequency Threshold", "ui_hint": "percentage", "min": 0.0, "max": 1.0, "step": 0.025}
    )

    min_games: int = field(
        default=20,
        metadata={"label": "Minimum Number of Games", "min": 1, "max": 1000}
    )

    min_observations: int = field(
        default=3,
        metadata={"label": "Min edge weight to be shown", "min": 1, "max": 100}
    )



feature_list = [CheckerOptions, GraphOptions, SpacedRepetitionOptions]

def save_settings(options_obj, options_class):
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            try:
                full_config = json.load(f)
            except json.JSONDecodeError:
                # full_config = {}
                raise
    else:
        full_config = {}
    
    core_field_names = {f.name for f in fields(CoreOptions)}
    current_data = asdict(options_obj)
    
    core_to_save = {k: v for k, v in current_data.items() if k in core_field_names}
    feature_to_save = {k: v for k, v in current_data.items() if k not in core_field_names}

    full_config["Core"] = core_to_save
    full_config[options_class.__name__] = feature_to_save

    full_config["feature_used"] = feature_list.index(options_class)

    with open(CONFIG_FILE, "w") as f:
        json.dump(full_config, f, indent=4)

DEFAULT_FEATURE_INDEX = 0

def load_settings(options_class = None):
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error loading settings: {e}")
    else: 
        print(f"No setting file found, using defaults")
        data = {}

    if not options_class:
        class_index = data.get("feature_used", DEFAULT_FEATURE_INDEX)
        options_class = feature_list[class_index]

    core_data = data.get("Core", {})
    feature_data = data.get(options_class.__name__, {})

    combined_data = {**core_data, **feature_data}

    valid_fields = {f.name for f in fields(options_class)}
    final_params = {k: v for k, v in combined_data.items() if k in valid_fields}

    return options_class(**final_params), options_class

def cache_filename_from_string(dir: str, string: str) -> str:
    base = os.path.join("cache", dir)
    name = sha1(string.encode()).hexdigest()[:10]
    return os.path.join(base, f"{name}.json")
