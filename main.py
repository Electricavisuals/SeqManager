import sys
import os

os.environ.setdefault("QT_QPA_PLATFORM", "windows:dpiawareness=0")
os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")


def main():
    args = sys.argv[1:]

    if args and args[0] == "--uninstall":
        from installer import uninstall
        uninstall()
        return

    if not args or args[0] == "--install":
        from installer import install
        install()
        return

    from pathlib import Path
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    import config as cfg

    app = QApplication.instance() or QApplication(sys.argv)
    ico = Path(__file__).parent / "ICON" / "SeqManager256.ico"
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))

    from viewer import run_viewer
    run_viewer(args[0], cfg.load(), app)


if __name__ == "__main__":
    main()
