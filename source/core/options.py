from dataclasses import dataclass, field, asdict, fields
import json
import os

from hashlib import sha1


CONFIG_FILE = "settings.json"

DEBUG_MODE = True

VALID_DB_RATINGS = ["1900", "2200", "2500"]
DEFAULT_DB_RATINGS = ["2200", "2500"]

DB_LICHESS = "db_lichess"
DB_MASTERS = "db_masters"
NO_MOVE_PROMOTION = "none"
MOVE_PROMOTION_CRITERIA = {
    "No Promotion": NO_MOVE_PROMOTION,
    "Masters Frequency": DB_MASTERS,
    "Lichess Frequency": DB_LICHESS,
}

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
        default_factory=lambda: [DB_LICHESS],
        metadata={"label": "Database Types", "options": {"Lichess": DB_LICHESS, "Masters": DB_MASTERS}}
    )

    db_ratings: list = field(
        default_factory=lambda: DEFAULT_DB_RATINGS.copy(),
        metadata={
            "label": "Lichess Ratings",
            "options": {rating: rating for rating in VALID_DB_RATINGS},
        }
    )

    def validate(self):
        if self.min_depth > self.max_depth:
            raise ValueError("min_depth must be ≤ max_depth")
        if not isinstance(self.db_ratings, list):
            raise TypeError("db_ratings must be a list")
        invalid_ratings = [rating for rating in self.db_ratings if rating not in VALID_DB_RATINGS]
        if invalid_ratings:
            raise ValueError(f"db_ratings must be a subset of {VALID_DB_RATINGS}; got {invalid_ratings}")
        if len(set(self.db_ratings)) != len(self.db_ratings):
            raise ValueError("db_ratings cannot contain duplicates")
        

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
            "min": 0,
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
            "max": 200,
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
    starting_fen: str = field(
        default="",
        metadata={"label": "Starting Position (FEN)"}
    )

    # --- WHAT TO DO WITH IT ---
    actions: list = field(
        default_factory=lambda: ["find_gaps"],
        metadata={"label": "Actions", "options": {"Find Gaps": "find_gaps", "Fill Gaps": "fill_gaps",
                                                   "Mark Moves": "mark_moves", "Seek Consistency": "seek_consistency"}}
    )

    move_promotion_criterion: str = field(
        default=NO_MOVE_PROMOTION,
        metadata={
            "label": "Move Promotion Criterion",
            "ui_hint": "dropdown",
            "options": MOVE_PROMOTION_CRITERIA,
            "ui_section": "Mark Moves Options",
        },
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
        if self.move_promotion_criterion not in MOVE_PROMOTION_CRITERIA.values():
            raise ValueError(
                "move_promotion_criterion must be one of "
                f"{list(MOVE_PROMOTION_CRITERIA.values())}; "
                f"got {self.move_promotion_criterion!r}"
            )


@dataclass
class SpacedRepetitionOptions(RepertoireOptions):
    starting_fen: str = field(
        default="",
        metadata={"label": "Starting Position (FEN)"}
    )
    
    start_range: int = field(
        default=5,
        metadata={"label": "Prompts ? Moves From Starting Pos", "min": 0, "max": 30,}
    )
    
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

    preload_db: bool = field(
        default=False,
        metadata={
            "label": "Preload DB Stats (makes init slow, but queries faster later)",
            "ui_hint": "checkbox",
        },
    )

    local_generation: bool = field(
        default=True,
        metadata={
            "label": "Generate Prompts Move by Move",
            "ui_hint": "checkbox",
        },
    )

    min_prompt_len : int = field(
        default=3,
        metadata={"label": "Minimum Prompt Length (Moves)", "min": 1, "max": 10}
    )

    max_prompt_len : int = field(
        default=7,
        metadata={"label": "Maximum Prompt Length (Moves)", "min": 1, "max": 20}
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
    
    starting_fen: str = field(
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
