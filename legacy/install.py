#!/usr/bin/env python3
"""Copy this package into a Hermes profile without overwriting code or state."""
import argparse
import os
from pathlib import Path
import shutil
import tempfile


def install(source, home):
    plugins = home.expanduser().resolve() / 'plugins'
    plugins.mkdir(parents=True, exist_ok=True)
    target = plugins / 'mailbridge'
    if target.exists() or target.is_symlink():
        raise ValueError(f'Already installed at {target}; move the old code aside before installing. Plugin data stays in plugin-data/mailbridge.')
    if plugins.is_relative_to(source.resolve()):
        raise ValueError('Hermes home cannot be inside the package being copied')
    with tempfile.TemporaryDirectory(prefix='.mailbridge-install-', dir=plugins) as tmp:
        staged = Path(tmp) / 'mailbridge'
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns(
            '.git', '.venv', '__pycache__', '*.pyc', 'runtime', '.pytest_cache', '.gstack'))
        staged.rename(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-home', type=Path,
        default=Path(os.environ.get('HERMES_HOME', '~/.hermes')).expanduser())
    args = parser.parse_args()
    try:
        target = install(Path(__file__).resolve().parent, args.hermes_home)
    except ValueError as exc:
        parser.error(str(exc))
    print(f'Installed code: {target}')
    print(f'Next: HERMES_HOME="{target.parent.parent}" hermes plugins enable mailbridge')
    print('Then run: hermes plugins doctor mailbridge')


if __name__ == '__main__':
    main()
