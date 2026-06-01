import sys
import winreg
import ctypes
from pathlib import Path


_REG_ROOTS = [
    r"Software\Classes\Directory\shell\SeqManager",
    r"Software\Classes\Directory\Background\shell\SeqManager",
]


def _exe_path() -> str:
    return str(Path(sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]).resolve())


def _icon_path() -> str:
    if getattr(sys, 'frozen', False):
        bases = [Path(sys._MEIPASS), Path(sys.executable).parent]
    else:
        bases = [Path(sys.argv[0]).resolve().parent]
    for base in bases:
        for candidate in ["SeqManager256.ico", "ICON/SeqManager256.ico", "icon.ico"]:
            ico = base / candidate
            if ico.exists():
                return str(ico)
    return _exe_path()


def _msgbox(title: str, text: str):
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x40 | 0x40000 | 0x10000)


def _show_install_ok():
    from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QPushButton
    from PySide6.QtGui import QIcon
    from PySide6.QtCore import Qt

    app = QApplication.instance() or QApplication(sys.argv)

    dlg = QDialog()
    dlg.setWindowTitle("SeqManager")
    dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    dlg.setFixedWidth(280)
    dlg.setStyleSheet("""
        QDialog {
            background-color: #1a1a22;
            border: 1px solid #3a3a50;
        }
        QLabel { color: #e8e8f0; font-family: 'IBM Plex Mono', monospace; font-size: 11px; }
        QPushButton {
            background-color: #3a6e87; color: #e8f4f8;
            border: none; border-radius: 3px;
            padding: 6px 20px; font-weight: bold;
            font-family: 'IBM Plex Mono', monospace; font-size: 11px;
        }
        QPushButton:hover { background-color: #4a7e97; }
    """)

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 24, 24, 20)
    layout.setSpacing(12)

    msg = QLabel("SeqManager is correctly installed.")
    msg.setAlignment(Qt.AlignCenter)
    msg.setStyleSheet("color: #c8e8f8; font-size: 12px; font-weight: bold;")
    layout.addWidget(msg)

    sub = QLabel("You'll find it on the folder context menu.")
    sub.setAlignment(Qt.AlignCenter)
    sub.setStyleSheet("color: #9ab8c8; font-size: 10px;")
    layout.addWidget(sub)

    ico_lbl = QLabel()
    ico_lbl.setAlignment(Qt.AlignCenter)
    pix = QIcon(_icon_path()).pixmap(64, 64)
    if not pix.isNull():
        ico_lbl.setPixmap(pix)
    layout.addWidget(ico_lbl)

    thanks = QLabel("Hope you enjoy it!\nThanks!  A.C.")
    thanks.setAlignment(Qt.AlignCenter)
    thanks.setStyleSheet("color: #9ab8c8; font-size: 10px;")
    layout.addWidget(thanks)

    btn = QPushButton("OK")
    btn.clicked.connect(app.quit)
    layout.addWidget(btn, alignment=Qt.AlignCenter)

    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    app.exec()


def _pythonw_path() -> str:
    p = Path(sys.executable)
    # Try pythonw.exe (Windows: hides console window)
    w = p.parent / p.name.lower().replace('python', 'pythonw', 1)
    if w.exists():
        return str(w)
    return str(p)


def _command(arg: str) -> str:
    if getattr(sys, 'frozen', False):
        exe = _exe_path()
        return f'"{exe}" {arg}'
    else:
        python = _pythonw_path()
        script = str(Path(sys.argv[0]).resolve())
        return f'"{python}" "{script}" {arg}'


def install():
    for root in _REG_ROOTS:
        arg = '"%V"' if "Background" in root else '"%1"'
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "SEQ-MANAGER")
                # Frozen exe: icon is embedded in the exe itself (PyInstaller icon=)
                # Script: point to the .ico file on disk
                if getattr(sys, 'frozen', False):
                    icon_val = _exe_path() + ",0"
                else:
                    ico = _icon_path()
                    icon_val = ico if ico.lower().endswith(".ico") else ico + ",0"
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon_val)
                winreg.SetValueEx(k, "Position", 0, winreg.REG_SZ, "Top")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + r"\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _command(arg))
        except OSError as e:
            _msgbox("SeqManager — Error", f"Installation failed:\n{e}")
            return
    _show_install_ok()


def uninstall():
    for root in _REG_ROOTS:
        for subkey in (root + r"\command", root):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
            except FileNotFoundError:
                pass
            except OSError as e:
                _msgbox("SeqManager — Error", f"Uninstall failed:\n{e}")
                return
    _msgbox("SeqManager", "Successfully uninstalled.")
