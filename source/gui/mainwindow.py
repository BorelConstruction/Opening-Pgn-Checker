from dataclasses import fields
from pathlib import Path

import chess
from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QGroupBox
)

from ..core import pgnChecker as checker
from ..core.options import Options, load_settings, save_settings
from ..core.pgnChecker import CheckerReport
from os import getenv

# from core.engine import run_engine

MAX_ROWS = 7


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.options = load_settings()
        self.setWindowTitle("Opening Tool")
        self.options_class = Options
        self.widgets = {}  # Store widgets to retrieve values later
        self.init_ui()
        # QCoreApplication.instance().aboutToQuit.connect(self.controller.shutdown)



    def init_ui(self):
        main_layout = QHBoxLayout()
        options_layout = QVBoxLayout()
        self.grid_layout = QGridLayout()
        options_layout.addLayout(self.grid_layout)
        # form_layout = QFormLayout()

        # input pgn
        row = QHBoxLayout()
        row.addStretch(1)

        label = QLabel("Input PGN:")
        self.input_pgn_path = QLineEdit()
        self.input_pgn_path.setPlaceholderText("Select a PGN file…")
        if self.options.input_pgn:
            self.input_pgn_path.setText(self.options.input_pgn)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_opening)


        row.addWidget(label)
        row.addWidget(browse)
        row.addWidget(self.input_pgn_path)
        # options_layout.addLayout(self.opening_path)
        options_layout.addLayout(row)

        # run
        run = QPushButton("Run")
        run.clicked.connect(self.on_run)
        options_layout.addWidget(run)

        # reset settings
        reset = QPushButton("Reset")
        reset.clicked.connect(self.reset_to_defaults)
        options_layout.addWidget(reset)

        # progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        options_layout.addWidget(self.progress_bar)
        self.progress_bar.setFormat("%v / %m") # to show absolute values

        # board
        self.board = BoardWidget()
        self.board.setVisible(False)
        self.board.setSizePolicy(
            QSizePolicy.Expanding,   # horizontal
            QSizePolicy.Fixed        # vertical
        )

        # text feedback
        self.feedback = QTextEdit()
        self.feedback.setReadOnly(True)
        self.feedback.setMaximumHeight(120)
        self.feedback.setVisible(False)
        # font = QFont("Consolas")      # "Courier New", "Monospace"
        # font.setPointSize(13)
        # self.feedback.setFont(font)
        self.feedback.setStyleSheet("""
            QTextEdit {
                font-family: Consolas, Monaco, monospace;
                font-size: 13pt;
                color: #202020;
                background-color: #f4f4f4;
                border: 1px solid #c0c0c0;
                border-radius: 4px;
                padding: 4px;
            }
            """)


        i = 0
        for field in fields(self.options_class):
            if field.metadata.get("ui_hint") == "manually":
                continue

            name = field.name
            val = getattr(self.options, field.name)
            label = field.metadata.get("label", field.name.replace("_", " ").title())
            widget = create_widget_for_field(field, val)

            col = i // MAX_ROWS
            row = (i % MAX_ROWS) * 2
            self.grid_layout.addWidget(QLabel(label), row, col)
            self.grid_layout.addWidget(widget, row+1, col)
            self.widgets[field.name] = widget

            # form_layout.addRow(name.replace("_", " ").title(), widget)
            self.widgets[name] = widget

            i += 1
        # options_layout.addLayout(form_layout)
        right_layout = QVBoxLayout()
        right_layout.addWidget(self.board)
        right_layout.addWidget(self.feedback)
        self.right_widget = QWidget()
        self.right_widget.setLayout(right_layout)

        options_widget = QWidget()
        options_widget.setLayout(options_layout)
        main_layout.addWidget(options_widget, stretch=0)
        main_layout.addWidget(self.right_widget, stretch=1)
        self.setLayout(main_layout)

    def get_current_options(self):
        """Helper to extract the data back into a Options object"""
        data = {}
        for field in fields(self.options_class):
            name = field.name
            if name in self.widgets:
                widget = self.widgets[name]
                if isinstance(widget, QSpinBox) or isinstance(widget, QDoubleSpinBox):
                    data[name] = field.type(widget.value())
                elif isinstance(widget, QCheckBox):
                    data[name] = widget.isChecked()
                elif isinstance(widget, QGroupBox):
                    data[name] = widget.get_value()
                else:
                    data[name] = widget.text()
        return self.options_class(**data)

    def browse_opening(self):
        project_root = Path(__file__).resolve().parents[2]
        start_dir = project_root / "input pgns"
        path, _ = QFileDialog.getOpenFileName(
            self, "Select opening PGN", str(start_dir), "PGN files (*.pgn)"
        )
        if path:
            self.input_pgn_path.setText(path)

    def on_run(self):
        self.progress_bar.setValue(0)
        self.show_runtime_widgets()


        self.options = self.get_current_options()
        if self.input_pgn_path.text():
            self.options.input_pgn = self.input_pgn_path.text()
        # side = chess.WHITE if self.white_radio.isChecked() else chess.BLACK # TODO: get rid of chess import?
        self.options.validate()
        save_settings(self.options)

        self.setEnabled(False)

        self.thread = QThread(self)
        self.worker = EngineWorker(self.options)
        if not getenv("APP_DEBUG") == "1":
            self.worker.moveToThread(self.thread)

        # connections
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.report.connect(self.on_engine_report)

        # cleanup (very important)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self.thread.start()
        # self.thread.run()


    def on_finished(self, report: str):
        self.hide_runtime_widgets()
        self.setEnabled(True)
        QMessageBox.information(self, "Analysis finished.", report)

    def on_error(self, message):
        self.setEnabled(True)
        self.hide_runtime_widgets()
        QMessageBox.critical(self, "Engine error", message)

    def on_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def on_engine_report(self, report: CheckerReport):
        self.board.setVisible(True)
        self.feedback.setVisible(True)
        if report.position:
            self.board.show_report(report, orientation=chess.WHITE if self.options.play_white else chess.BLACK)
        # sys.stderr.write(str(report.message))
        self.feedback.setPlainText(report.message)

    def hide_runtime_widgets(self):
        self.progress_bar.setVisible(False)
        self.right_widget.setVisible(False)
        self.board.setVisible(False)
        self.feedback.setVisible(False)

    def show_runtime_widgets(self):
        self.progress_bar.setVisible(True)
        self.right_widget.setVisible(True)

    def reset_to_defaults(self):
        for f in fields(Options):
            if f.name in self.widgets:
                widget = self.widgets[f.name]
                if isinstance(widget, QSpinBox) or isinstance(widget, QDoubleSpinBox):
                    widget.setValue(f.default)
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(f.default)
                elif isinstance(widget, QGroupBox):
                    widget.set_value(f.default_factory())
                else:
                    widget.setText(f.default)

import sys
class EngineWorker(QObject):
    finished = Signal(str)
    error = Signal(str)
    progress = Signal(int, int)
    report = Signal(object)

    def __init__(self, options):
        super().__init__()
        self.options = options

    @Slot()
    def run(self):
        try:
            c = checker.PgnChecker(self.options, self.progress.emit, self.report.emit)
            report = c.run()
            # test.test(self.options)
            c.close()
            self.finished.emit(report)
        except Exception as e:
            self.error.emit(str(e))
            c.close()
            return
    

class BoardWidget(QSvgWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)

    def show_report(self, report : CheckerReport, orientation=chess.WHITE):
        if not report.position:
            self.setVisible(False)
            return
        self.setVisible(True)
        svg = chess.svg.board(
            board=chess.Board(report.position.fen),
            lastmove=chess.Move.from_uci(report.position.last_move_uci)
                    if report.position.last_move_uci else None,
            orientation=orientation,
            size=100
        )
        self.load(bytearray(svg, encoding="utf-8"))

    # def set_fen(self, fen: str):
    #     board = chess.Board(fen)
    #     self.set_board(board)


def create_widget_for_field(field_info, current_value):
    metadata = field_info.metadata
    v_type = field_info.type
    hint = metadata.get("ui_hint")

    # 1. Handle Categorical / Enums (The 'side' option)
    if hint == "dropdown":
        widget = QComboBox()
        options = metadata.get("options", {})
        for label, val in options.items():
            widget.addItem(label, val)
        # Set current selection
        index = widget.findData(current_value)
        widget.setCurrentIndex(index)
        return widget

    # 2. Handle Booleans
    if v_type is bool:
        widget = QCheckBox()
        widget.setChecked(current_value)
        return widget

    # 3. Handle Integers
    if v_type is int:
        widget = QSpinBox()
        widget.setRange(metadata.get("min", 0), metadata.get("max", 999))
        widget.setValue(current_value)
        return widget

    # 4. Handle Floats (Thresholds)
    if v_type is float:
        widget = QDoubleSpinBox()
        widget.setRange(metadata.get("min", 0.0), metadata.get("max", 1.0))
        widget.setSingleStep(metadata.get("step", 0.1))
        widget.setValue(current_value)
        return widget

    if v_type == list:
        widget = create_selector(metadata["options"])
        widget.set_value(current_value)
        return widget

    # 5. Default: Text/Paths
    widget = QLineEdit()
    widget.setText(str(current_value))
    return widget

def create_selector(options: dict):

    container = QGroupBox("Select Databases")
    layout = QHBoxLayout(container) # Horizontal looks better for small lists
    
    checkboxes = []

    def on_checkbox_toggled():
        checked_count = sum(1 for cb in checkboxes if cb.isChecked())
        
        # If only one checkbox is checked, disable it so it cannot be unchecked
        if checked_count == 1:
            for cb in checkboxes:
                if cb.isChecked():
                    cb.setEnabled(False)
        else:
            # Re-enable all if more than one is checked
            for cb in checkboxes:
                cb.setEnabled(True)

    for name in options:
        cb = QCheckBox(name)
        # if name in default_selected:
        #     cb.setChecked(True)
        
        # to force one of the checkboxes to be checked -- won't use now as it can be used w/o dbs at all, in theory
        # cb.toggled.connect(on_checkbox_toggled)
        checkboxes.append(cb)
        layout.addWidget(cb)

    # Initial validation run
    # on_checkbox_toggled()
    
    # we won't follow Qt's naming as 1. it is not pythonic 2. we emphasize this is monkey-patched
    container.get_value = lambda: [options[cb.text()] for cb in checkboxes if cb.isChecked()]
    container.set_value = lambda l: [cb.setChecked(True) for cb in checkboxes if options[cb.text()] in l]

    
    return container