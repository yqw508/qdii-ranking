"""Build the reproducible HTTP function package without changing public files."""
import argparse
import shutil
import subprocess
from pathlib import Path

from qdii_ranking.premium_service import validate_snapshot
from qdii_validation.schema import load_payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('output/qdii-ranking'))
    parser.add_argument('--publish-dir', type=Path, default=Path('public'))
    parser.add_argument('--target', type=Path, default=Path('tmp/premium-function'))
    args = parser.parse_args()
    payload = load_payload(args.output_dir / 'latest.json')
    validate_snapshot(args.publish_dir / 'premium-snapshot.json', payload)
    args.target.mkdir(parents=True, exist_ok=True)
    for path in Path('functions/qdii-premium-api').iterdir():
        if path.is_file():
            shutil.copy2(path, args.target / path.name)
    shutil.copy2(args.publish_dir / 'premium-snapshot.json', args.target / 'seed.json')
    bootstrap = args.target / 'scf_bootstrap'
    bootstrap.write_bytes(bootstrap.read_bytes().replace(b'\r\n', b'\n'))
    bootstrap.chmod(0o755)
    subprocess.run([shutil.which('npm') or 'npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'],
                   cwd=args.target, check=True)
    print(f'Prepared {args.target}')


if __name__ == '__main__':
    main()
