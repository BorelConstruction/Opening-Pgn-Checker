# gui/main.py
import sys
from PySide6.QtWidgets import QApplication
from .mainwindow import MainWindow

app = QApplication(sys.argv)
w = MainWindow()
w.show()
sys.exit(app.exec())
