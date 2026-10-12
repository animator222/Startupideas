"""Deadlock Clipper entry point (started by the desktop shortcut)."""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _fatal(message):
    """Last-resort error popup that needs nothing but Windows itself."""
    try:
        from clipper.paths import LOG_DIR
        with open(LOG_DIR / "crash.log", "a", encoding="utf-8") as f:
            f.write(message + "\n")
        where = str(LOG_DIR / "crash.log")
    except Exception:
        where = "(could not write log)"
    text = f"Deadlock Clipper couldn't start.\n\nTry running Install.bat again.\nDetails: {where}\n\n{message[-1500:]}"
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Deadlock Clipper", 0x10)
    else:
        print(text, file=sys.stderr)


def main():
    from clipper.log import setup_logging
    setup_logging()

    import logging
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from clipper import APP_NAME
    from clipper.ffmpeg import ffmpeg_version
    from clipper.gui.common import STYLE
    from clipper.gui.main_window import MainWindow

    logging.getLogger("startup").info("ffmpeg: %s", ffmpeg_version())
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")))
    from clipper.textrender import font_families
    font_families()  # register the bundled fonts on the main thread
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        window.open_file(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        _fatal(traceback.format_exc())
        sys.exit(1)
