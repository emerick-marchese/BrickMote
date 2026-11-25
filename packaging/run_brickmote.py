"""PyInstaller entry point for the BrickMote desktop application."""

import multiprocessing
import sys

from brickmote.gui.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
