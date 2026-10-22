"""Read-only check of the published release through the production updater."""
import argparse
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from steam_sda import __version__
from steam_sda.infrastructure.updates.github import GitHubUpdates


def verify(previous):
    with tempfile.TemporaryDirectory(prefix='SteamSDA-update-feed-') as directory:
        gateway = GitHubUpdates(directory)
        release = gateway.latest(previous)
        if not release or release.version != __version__:
            raise RuntimeError('Published update feed does not identify this release.')
        path = gateway.download(release)
        if not gateway._verified(Path(path), release):
            raise RuntimeError('Published updater download failed verification.')
        print(f'Production update feed and installer download verified: {release.version}, {release.size} bytes.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--previous', required=True)
    args = parser.parse_args()
    verify(args.previous)
