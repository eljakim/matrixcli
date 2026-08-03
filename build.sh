#!/usr/bin/env bash
# Build distributable artifacts into build/:
#
#   build/dist/matrixcli-<version>-py3-none-any.whl   pure-Python wheel (+ sdist)
#   build/matrixcli-<version>-<os>-<arch>.tar.gz      standalone bundle, no
#                                                     Python needed on the target
#   build/INSTALL.md                                  instructions to ship along
#
# The standalone bundle is built with PyInstaller and only runs on the same
# OS/architecture as the machine that built it. Run this script on a Linux box
# to produce the Linux bundle; the wheel from any run works everywhere.
#
# Usage: ./build.sh [--wheel-only]
set -euo pipefail

cd "$(dirname "$0")"

wheel_only=0
[ "${1:-}" = "--wheel-only" ] && wheel_only=1

version="$(poetry version -s)"
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$os" in
    darwin) platform="macos-$arch" ;;
    *)      platform="$os-$arch" ;;
esac

mkdir -p build
rm -rf build/dist build/pyinstaller
rm -f build/matrixcli-*.tar.gz

echo "==> Building wheel and sdist (build/dist/)"
poetry install --sync >/dev/null 2>&1 || poetry install >/dev/null
poetry build --output build/dist

if [ "$wheel_only" -eq 0 ]; then
    echo "==> Building standalone bundle for $platform"
    mkdir -p build/pyinstaller
    cat > build/pyinstaller/entry.py <<'PY'
from matrixcli.app import main

if __name__ == "__main__":
    main()
PY

    # keyring discovers its backends through entry points, which PyInstaller's
    # static analysis cannot see; name the platform backend explicitly.
    keyring_flags=(--copy-metadata keyring)
    if [ "$os" = "darwin" ]; then
        keyring_flags+=(--hidden-import keyring.backends.macOS)
    else
        keyring_flags+=(--hidden-import keyring.backends.SecretService)
    fi

    poetry run pyinstaller \
        --name matrix \
        --onefile \
        --noconfirm \
        --clean \
        --log-level WARN \
        --distpath build/pyinstaller/dist \
        --workpath build/pyinstaller/work \
        --specpath build/pyinstaller \
        --collect-all textual \
        --collect-all nio \
        --collect-all vodozemac \
        "${keyring_flags[@]}" \
        build/pyinstaller/entry.py

    tarball="build/matrixcli-$version-$platform.tar.gz"
    tar -C build/pyinstaller/dist -czf "$tarball" matrix
    echo "==> $tarball"

    if command -v shasum >/dev/null 2>&1; then
        ( cd build && shasum -a 256 "$(basename "$tarball")" > "$(basename "$tarball").sha256" )
    elif command -v sha256sum >/dev/null 2>&1; then
        ( cd build && sha256sum "$(basename "$tarball")" > "$(basename "$tarball").sha256" )
    fi
    echo "==> $tarball.sha256"
fi

cat > build/INSTALL.md <<EOF
# matrixcli $version

A terminal Matrix client (dashboard, threads, E2E encryption). Two ways to
install, pick one:

## Option A: standalone bundle (no Python required)

For the tarball matching your OS and CPU (e.g.
\`matrixcli-$version-$platform.tar.gz\`):

    tar xzf matrixcli-$version-<os>-<arch>.tar.gz
    ./matrix

First verify the download against the checksum published beside it (the build
emits \`matrixcli-$version-<os>-<arch>.tar.gz.sha256\`):

    shasum -a 256 -c matrixcli-$version-<os>-<arch>.tar.gz.sha256

Move the \`matrix\` binary anywhere on your PATH if you like. This bundle is
not code-signed or notarized, so on macOS Gatekeeper will refuse to run a
copy downloaded through a browser. Only after the checksum above matches,
clear the quarantine attribute:

    xattr -d com.apple.quarantine ./matrix

Do not run \`xattr\` on a bundle whose checksum you have not verified: it is
exactly the step that would let a tampered or swapped download execute.

The bundle only runs on the same OS/architecture it was built for. If there is
no tarball for your platform, use Option B, or run \`./build.sh\` from the
source tree on a machine of that platform to produce one.

## Option B: Python wheel (any OS, needs Python 3.10-3.14)

    pipx install matrixcli-$version-py3-none-any.whl

(or \`pip install\` into a virtualenv of your choice.)

End-to-end encryption is provided by vodozemac, which installs as a prebuilt
wheel alongside matrix-nio; no system libraries or compilers are needed.

## First run

Running \`matrix\` once writes a config template (to
\`~/.config/matrixcli/config.ini\`) and tells you what to fill in: your
homeserver, user id, and a login password stored in the system keyring
(macOS Keychain; on Linux a Secret Service such as GNOME Keyring or KWallet
must be available).

To decrypt message history older than this device, export your room keys
from another client (Element: Settings -> Security & Privacy -> Export E2E
room keys) and import them with \`matrix --import-keys FILE\`. Verify the
new session from Element with \`matrix --verify\`.
EOF

echo "==> Done. Artifacts in build/:"
ls -lh build/dist build/matrixcli-*.tar.gz 2>/dev/null | sed 's/^/    /'
echo "    build/INSTALL.md"
