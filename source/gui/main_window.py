from dataclasses import fields
from pathlib import Path

import chess
from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
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
    QGroupBox,
    QStackedWidget
)

from ..core.runner import Runner, RunnerReport
from ..core.options import *
from os import getenv

# from core.engine import run_engine

MAX_ROWS = 7

FEATURE_NAMES = {
    CheckerOptions: "Pgn Checker",
    GraphOptions: "Inclusion Graph",
    SpacedRepetitionOptions: "Spaced Repetition (Web Board)",
}

# Introduce feature specs if this grows
OPT_TO_FEATURE = {
    CheckerOptions: Runner,
    GraphOptions: Runner,
    SpacedRepetitionOptions: Runner,
}


class FilePickerWidget(QWidget):
    def __init__(self, label, file_filter="All Files (*.*)", initial_dir="."):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0) # Keep it tight for the grid

        # FORCE A SIZE FOR DEBUGGING
        self.setMinimumHeight(40)
        self.setMinimumWidth(200)
        
        self.line_edit = QLineEdit()
        self.line_edit.setPlaceholderText(f"Select {label}...")
        
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._do_browse)
        
        self.file_filter = file_filter
        self.initial_dir = initial_dir
        self.label = label

        layout.addWidget(self.line_edit)
        layout.addWidget(self.browse_btn)

    def _do_browse(self):
        # Resolve path relative to project root if needed
        start_path = str(Path(__file__).resolve().parents[2] / self.initial_dir)
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {self.label}", start_path, self.file_filter
        )
        if path:
            self.line_edit.setText(path)

    # Standardize the getter name to match QLineEdit
    def text(self):
        return self.line_edit.text()
        
    def setText(self, value):
        self.line_edit.setText(value)

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.options, self.options_class = load_settings()
        self.setWindowTitle("Opening Tool")
        # Active widget map for the currently-selected feature (== self.widgets_by_index[current index])
        self.widgets = {}
        # Per-feature widget maps keyed by the QStackedWidget index.
        self.widgets_by_index = {}
        self.init_ui()
        # QCoreApplication.instance().aboutToQuit.connect(self.controller.shutdown)



    def init_ui(self):
        main_layout = QHBoxLayout()
        options_layout = QVBoxLayout()
        # form_layout = QFormLayout()

        # run
        run = QPushButton("Run")
        run.clicked.connect(self.on_run)

        # reset settings
        reset = QPushButton("Reset")
        reset.clicked.connect(self.reset_to_defaults)

        # progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
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
        self.feedback.setMaximumHeight(200)
        self.feedback.setVisible(False)
        # font = QFont("Consolas")      # "Courier New", "Monospace"
        # font.setPointSize(13)
        # self.feedback.setFont(font)
        self.feedback.setStyleSheet("""
            QTextEdit {
                font-family: Consolas, Monaco, monospace;
                font-size: 11pt;
                color: #202020;
                background-color: #f4f4f4;
                border: 1px solid #c0c0c0;
                border-radius: 4px;
                padding: 4px;
            }
            """)
        
        self.feature_selector = QComboBox()
        self.feature_selector.addItems([FEATURE_NAMES[f] for f in feature_list])
        index = feature_list.index(self.options_class)
        self.feature_selector.setCurrentIndex(index)
        self.feature_selector.currentIndexChanged.connect(self.switch_feature)
        options_layout.addWidget(self.feature_selector)


        self.stack = QStackedWidget()
        for _ in range(self.feature_selector.count()):
            self.stack.addWidget(QWidget())

        self.pages = {} # Map: Feature -> Widget

        # load pages lazily; initially one only
        w = self.create_group_for_options(self.options_class)
        old = self.stack.widget(index)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.insertWidget(index, w)
        self.stack.setCurrentIndex(index)

        self.pages[index] = w
        self.widgets_by_index[index] = w._widgets
        self.widgets = self.widgets_by_index[index]

        options_layout.addWidget(self.stack)

        # lower part
        options_layout.addWidget(reset)
        options_layout.addWidget(run)
        options_layout.addWidget(self.progress_bar)


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

    def create_group_for_options(self, options_class, exclude_core=False):
        page_widget = QWidget()
        grid = QGridLayout(page_widget)
        page_widget._widgets = {}
        widgets = page_widget._widgets
        
        core_fields = {f.name for f in fields(CoreOptions)}

        all_fields = []
        for field in fields(options_class):
            if field.metadata.get("ui_hint") == "manually":
                continue
            if exclude_core and field.name in core_fields:
                continue  # Skip options already shown in the Core section
            all_fields.append(field)

        ui_group_fields = {}
        for field in all_fields:
            ui_group = field.metadata.get("ui_group")
            if not ui_group:
                continue
            ui_group_fields.setdefault(ui_group, []).append(field)

        rendered_ui_groups = set()
        i = 0
        for field in all_fields:
            ui_group = field.metadata.get("ui_group")
            if ui_group:
                if ui_group in rendered_ui_groups:
                    continue
                rendered_ui_groups.add(ui_group)

                group_fields = ui_group_fields.get(ui_group, [])
                group_fields = sorted(
                    group_fields,
                    key=lambda f: (f.metadata.get("ui_group_order", 0), all_fields.index(f)),
                )

                group_container = QWidget()
                hbox = QHBoxLayout(group_container)
                hbox.setContentsMargins(0, 0, 0, 0)

                for gf in group_fields:
                    val = getattr(self.options, gf.name)
                    label = gf.metadata.get("label", gf.name.replace("_", " ").title())
                    widget = create_widget_for_field(gf, val, label=label)

                    field_container = QWidget()
                    vbox = QVBoxLayout(field_container)
                    vbox.setContentsMargins(0, 0, 0, 0)
                    vbox.addWidget(QLabel(label))
                    vbox.addWidget(widget)

                    hbox.addWidget(field_container)
                    widgets[gf.name] = widget

                i += 1

                col = i // MAX_ROWS
                row = (i % MAX_ROWS) * 2
                grid.addWidget(group_container, row, col, 2, 1)
                continue

            name = field.name
            val = getattr(self.options, field.name)
            label = field.metadata.get("label", field.name.replace("_", " ").title())
            widget = create_widget_for_field(field, val, label=label)

            i += 1

            col = i // MAX_ROWS
            row = (i % MAX_ROWS) * 2
            grid.addWidget(QLabel(label), row, col)
            grid.addWidget(widget, row+1, col)
            widgets[field.name] = widget

        grid.setRowStretch(row, 1)
              
        return page_widget

    def switch_feature(self, index):
        self.options_class = feature_list[index]
        created_page = False

        if index not in self.pages:
            created_page = True
            self.options, _ = load_settings(self.options_class)
            w = self.create_group_for_options(self.options_class)

            old = self.stack.widget(index)
            self.stack.removeWidget(old)
            old.deleteLater()
            self.stack.insertWidget(index, w)

            self.pages[index] = w
            self.widgets_by_index[index] = w._widgets

        self.stack.setCurrentIndex(index)
        self.widgets = self.widgets_by_index[index]

        if not created_page:
            # Keep 'self.options' in sync with what's currently in the UI for this feature.
            self.options = self.get_current_options()



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
                elif hasattr(widget, "text"): 
                    data[name] = widget.text()
                else:
                    data[name] = widget.text()
        return self.options_class(**data)


    def on_run(self):
        try:
            self.options = self.get_current_options()
            self.save_settings()

            self.options.validate()

            if isinstance(self.options, SpacedRepetitionOptions):
                self.launch_spaced_repetition(self.options)
                return

            self.progress_bar.setValue(0)
            self.show_runtime_widgets()

            self.setEnabled(False)

            self.thread = QThread(self)
            runner = OPT_TO_FEATURE[self.options_class]
            self.worker = EngineWorker(self.options, runner)
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
        except Exception:
            # Catch synchronous errors before the worker thread even starts
            self.setEnabled(True)
            self.hide_runtime_widgets()
            e = sys.exc_info()[1]
            self.report_error(f"{e}")
            if DEBUG_MODE:
                raise
            return

    def launch_spaced_repetition(self, options: "SpacedRepetitionOptions") -> None:
        from ..web.server import ensure_web_server
        from ..web.app import sr_controller
        from ..web.spaced_repetition import SpacedRepetitionConfig

        handle = ensure_web_server(host="127.0.0.1", port=8000)
        cfg = SpacedRepetitionConfig(
            input_pgn=options.input_pgn,
            play_white=options.play_white,
            start_move=options.start_move,
            end_move=options.end_move,
            non_file_move_frequency=options.non_file_move_frequency,
            engine_path=options.engine_path,
        )
        sr_controller.start(cfg)
        QDesktopServices.openUrl(QUrl(handle.url))

    def save_settings(self):
        save_settings(self.options, self.options_class)

    def on_finished(self, report: str):
        self.hide_runtime_widgets()
        self.setEnabled(True)
        QMessageBox.information(self, "Analysis finished.", report)

    def report_error(self, message):
        QMessageBox.critical(self, "Error", message)

    def on_error(self, message):
        self.setEnabled(True)
        self.hide_runtime_widgets()
        self.report_error(message)

    def on_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def on_engine_report(self, report: RunnerReport):
        self.board.setVisible(True)
        self.feedback.setVisible(True)
        if report.position:
            if hasattr(self.options, "play_white"):
                orientation=chess.WHITE if self.options.play_white else chess.BLACK
            else:
                orientation=chess.WHITE
            self.board.show_report(report, orientation=orientation)
        # sys.stderr.write(str(report.message))
        if report.message:
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
        active_feature = feature_list[self.stack.currentIndex()]
        for f in fields(active_feature):
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

    def __init__(self, options, runner_cls):
        super().__init__()
        self.options = options
        self.runner_cls = runner_cls

    @Slot()
    def run(self):
        r = None
        report = None
        try:
            r = self.runner_cls(self.options, self.progress.emit, self.report.emit)
            print(self.options)
            report = r.run()
        except Exception as e:
            self.error.emit(f"{e}")
            return
        finally:
            if r is not None:
                r.close()

        self.finished.emit(report)
    

class BoardWidget(QSvgWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)

    def show_report(self, report : RunnerReport, orientation=chess.WHITE):
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


def create_widget_for_field(field_info, current_value, label=None):
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
    
    if hint == "file_path":
        f_filter = metadata.get("file_filter", "PGN files (*.pgn)")
        f_dir = metadata.get("initial_dir", "input_pgn")
        
        widget = FilePickerWidget(label or field_info.name, f_filter, f_dir)
        widget.setText(current_value)
        return widget

    if v_type is bool:
        widget = QCheckBox()
        widget.setChecked(current_value)
        return widget

    if v_type is int:
        widget = QSpinBox()
        widget.setRange(metadata.get("min", 0), metadata.get("max", 999))
        widget.setValue(current_value)
        return widget

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

    # Default: Text/Paths
    widget = QLineEdit()
    widget.setText(str(current_value))
    return widget

def browse_file(widget, dir: str):
    project_root = Path(__file__).resolve().parents[2]
    start_dir = project_root / dir
    path, _ = QFileDialog.getOpenFileName(
        widget, "Select opening PGN", str(start_dir), "PGN files (*.pgn)"
    )
    if path:
        widget.setText(path)

def create_selector(options: dict):

    container = QGroupBox("Select One or More")
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
