from dataclasses import dataclass, field, asdict
import chess
import json
import os

# @dataclass(frozen=True)
# class Defaults:
#     min_depth: int = 10
#     max_depth: int = 20
#     engine_path: str = "C:\\Users\\Vadim\\Downloads\\stockfish-windows-x86-64-avx2.exe"
#     side: chess.Color = chess.WHITE
#     start_ply: int = 10
#     end_ply: int = 40
#     freq_threshold: float = 0.3
#     obv_threshold: float = 0.8
#     add_nag: bool = True
#     trim_obvious_moves: bool = True
#     output_pgn: str = "Output.pgn"
    
#     # enable_tablebases: bool = False
#     # threads: int = 4

CONFIG_FILE = "settings.json"

@dataclass
class Options:
    input_pgn: str = field(
        default='',  # so it doesn't complain if we try to initialize an "empty" one
        metadata={"label": "Input PGN", "ui_hint": "manually"}
    )

    # --- ENGINE GROUP ---
    min_depth: int = field(
        default=12,
        metadata={"label": "Minimum Engine Depth", "min": 1, "max": 40, "group": "Engine Settings", "order": 1}
    )
    max_depth: int = field(
        default=30,
        metadata={"label": "Maximum Engine Depth", "min": 1, "max": 60, "group": "Engine Settings", "order": 2}
    )

    # --- MOVE CHOICE ---
    min_games: int = field(  # book cutoff
        default=10,
        metadata={"label": "Minimum Number of Games", "min": 1, "max": 500}
    )
    freq_threshold: float = field(
        default=0.15,
        metadata={"label": "Frequency Threshold", "ui_hint": "percentage", "min": 0.0, "max": 1.0, "step": 0.025}
    )

    added_depth: int = field(
        default=5,
        metadata={"label": "Length of Suggested Lines", "min": 1, "max": 20}
    )

    # File paths - needs a "Browse" button in GUI
    # engine_path: str = field(    # not really an "option"
    #     default="C:\\Users\\Vadim\\Downloads\\stockfish-windows-x86-64-avx2.exe",
    #     metadata={"label": "Engine Path", "ui_hint": "file_path"}
    # )


    # Game phase constraints - Range or SpinBox
    start_ply: int = field(
        default=10,
        metadata={"label": "Start Analysis at Ply", "min": 0, "max": 60}
    )
    end_ply: int = field(
        default=40,
        metadata={"label": "End Analysis at Ply", "min": 0, "max": 60}
    )

    # Choice/Enum - needs a ComboBox/Dropdown, but we implement this via checkbox
    play_white: bool = field(
        default=True,
        metadata={
            "label": "Play As White",
            # "ui_hint": "dropdown",
            # "options": {"White": chess.WHITE, "Black": chess.BLACK}
        }
    )

    obv_threshold: float = field(
        default=0.8,
        metadata={
            "label": "Obviousness Threshold", 
            "ui_hint": "percentage", 
            "min": 0.0, "max": 1.0, "step": 0.05}
    )

    # Booleans - simple Checkboxes
    add_nag: bool = field(
        default=True,
        metadata={"label": "Add NAG Annotations (+-, !?, etc.)"}
    )
    trim_obvious_moves: bool = field(
        default=True,
        metadata={"label": "Trim Obvious Moves"}
    )

    check_alternatives: bool = field(
        default=False,
        metadata={"label": "Check Alternatives"}
    )
    use_engine_for_them: bool = field(
        default=False,
        metadata={"label": "Engine for Opponent's Move"}
    )

    starting_pos: str = field(
        default="",
        metadata={"label": "Starting Position (FEN)"}
    )

    # Output file - another file path, but for saving
    output_pgn: str = field(
        default="Output.pgn",
        metadata={"label": "Output PGN Filename", "ui_hint": "save_file"}
    )

    _token: str = field(
        default="",
        metadata={"label": "Lichess API Token"}
    )

    def validate(self):
        if self.min_depth > self.max_depth:
            raise ValueError("min_depth must be ≤ max_depth")
        if not self.input_pgn:
            raise ValueError("No opening PGN selected")
        
def save_settings(self):
    with open(CONFIG_FILE, "w") as f:
        json.dump(asdict(self), f, indent=4)

def load_settings() -> Options:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                return Options(**data)
        except Exception as e:
            print(f"Error loading settings: {e}")
    return Options() # Fallback to hardcoded defaults