"""Resolve resources consistently from source and a PyInstaller distribution."""
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resource_path(relative):
    return Path(getattr(sys, '_MEIPASS', PROJECT_ROOT)) / relative


def helper_path():
    return resource_path('steamkit/SteamSDA.Auth.exe' if hasattr(sys, '_MEIPASS')
                         else 'build/steamkit/SteamSDA.Auth.exe')
