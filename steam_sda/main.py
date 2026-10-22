"""Command-line entry point; Qt initialization lives in presentation."""
def main():
    from .presentation.qt.app import main as qt_main
    return qt_main()
