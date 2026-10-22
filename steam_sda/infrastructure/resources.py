"""Resolve resources consistently from source and a PyInstaller distribution."""
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resource_path(relative):
    return (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else PROJECT_ROOT) / relative


def helper_path():
    return resource_path('integrations/steamkit/SteamSDA.Auth.exe' if getattr(sys, 'frozen', False)
                         else 'build/integrations/steamkit/SteamSDA.Auth.exe')


def device_path():
    return resource_path('integrations/device/SteamSDADevice.dll' if getattr(sys, 'frozen', False)
                         else 'build/integrations/device/SteamSDADevice.dll')
