#!/usr/bin/env bash
# Paper Isles v0.4.2 - build a real standalone macOS Finder application.
# Build-machine prerequisites are never required on end-user Macs.
set -Eeuo pipefail
# Do not package unrelated modules from the build machine.
unset PYTHONPATH PYTHONHOME

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(cat "$ROOT/VERSION")"
APP_NAME='纸屿 Paper Isles'
APP_ID='app.paperisles.localdesktop'
ICON="$ROOT/assets/PaperIsles-transparent.icns"
APP_ENTRY="$ROOT/app/app.py"
ARCH="$(uname -m)"
OUT="${PAPER_ISLES_OUT_DIR:-$ROOT/release/$ARCH}"
BUILD="$ROOT/.build/$ARCH"
VENV="$BUILD/venv"
APP_SIGN="${PAPER_ISLES_APP_SIGN_ID:-}"
INSTALLER_SIGN="${PAPER_ISLES_INSTALLER_SIGN_ID:-}"
NOTARIZE="${PAPER_ISLES_NOTARY:-0}"

if [[ "${1:-}" == '--help' ]]; then
    cat <<'EOF'
Build Paper Isles self-contained macOS desktop application.
  bash macos/build_macos.sh
Outputs: a standalone .app (inside .build), a distributable App.zip,
         a graphical .pkg installer with desktop shortcut attempt, and a .dmg.
Build machine: macOS plus Python 3.13. End users: no Python or pip needed.
Optional: PAPER_ISLES_APP_SIGN_ID, PAPER_ISLES_INSTALLER_SIGN_ID,
          PAPER_ISLES_NOTARY=1 and Apple notarization credentials.
EOF
    exit 0
fi
if [[ "$(uname -s)" != 'Darwin' ]]; then
    echo '[ERROR] A real macOS .app must be built on macOS (local Mac or CI runner).' >&2
    exit 2
fi
if [[ "$ARCH" != 'arm64' && "$ARCH" != 'x86_64' ]]; then
    echo "[ERROR] Unsupported Mac architecture: $ARCH" >&2
    exit 2
fi
if [[ ! -s "$ICON" || ! -f "$APP_ENTRY" || ! -f "$ROOT/app/static/index.html" ]]; then
    echo '[ERROR] App source, interface or original sailing icon is missing.' >&2
    exit 2
fi
if [[ -n "$APP_SIGN" && -z "$INSTALLER_SIGN" ]] || [[ -z "$APP_SIGN" && -n "$INSTALLER_SIGN" ]]; then
    echo '[ERROR] Provide BOTH Developer ID Application and Installer certificates.' >&2
    exit 2
fi
if [[ "$NOTARIZE" != 0 && "$NOTARIZE" != 1 ]]; then
    echo '[ERROR] PAPER_ISLES_NOTARY must be 0 or 1.' >&2
    exit 2
fi
if [[ "$NOTARIZE" == 1 ]]; then
    if [[ -z "$APP_SIGN" || -z "$INSTALLER_SIGN" || -z "${APPLE_NOTARY_APPLE_ID:-}" || \
          -z "${APPLE_NOTARY_APP_SPECIFIC_PASSWORD:-}" || -z "${APPLE_TEAM_ID:-}" ]]; then
        echo '[ERROR] Notarization needs signing identities and notary credentials.' >&2
        exit 2
    fi
    CHANNEL='RELEASE'
elif [[ -n "$APP_SIGN" ]]; then
    CHANNEL='SIGNED-NOT-NOTARIZED'
else
    CHANNEL='DEVELOPMENT-UNNOTARIZED'
fi

PYTHON="${PAPER_ISLES_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1 || ! "$PYTHON" -c \
    'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)'; then
    echo '[ERROR] Python 3.13 is needed on the BUILD machine only.' >&2
    echo '        Alternatively run the supplied GitHub macOS build workflow.' >&2
    exit 2
fi
mkdir -p "$BUILD" "$OUT"
if [[ ! -x "$VENV/bin/python" ]]; then "$PYTHON" -m venv "$VENV"; fi
PY="$VENV/bin/python"

notarize_file() {
    local file="$1"
    xcrun notarytool submit "$file" \
        --apple-id "$APPLE_NOTARY_APPLE_ID" \
        --password "$APPLE_NOTARY_APP_SPECIFIC_PASSWORD" \
        --team-id "$APPLE_TEAM_ID" --wait
}

check_bundled_runtime() {
    local app_to_test="${1:-$BUNDLE}"
    local test_home="$BUILD/selfcheck-$(date +%s)-$RANDOM"
    mkdir -p "$test_home"
    # PATH deliberately excludes Homebrew/python installations. The packaged
    # interpreter, native extensions, UI and PDF engine must work on their own.
    env -i HOME="$HOME" PATH='/usr/bin:/bin:/usr/sbin:/sbin' \
        LANG='en_US.UTF-8' PAPER_ISLES_HOME="$test_home" \
        "$app_to_test/Contents/MacOS/$APP_NAME" --self-check
    if [[ ! -f "$test_home/paper_isles.sqlite3" ]]; then
        echo '[ERROR] Frozen app could not initialize an isolated user library.' >&2
        exit 3
    fi
    rm -rf "$test_home"
}

echo '[1/8] Setting up build-only dependencies...'
"$PY" -m pip install --disable-pip-version-check -q --upgrade pip
"$PY" -m pip install --disable-pip-version-check -q -r "$ROOT/requirements-build-macos.txt"

echo '[2/8] Running code, PDF editing and Cocoa import checks...'
"$PY" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
"$PY" -c 'import pymupdf, webview, objc, WebKit, AppKit; print("Cocoa/PDF imports OK")'
if command -v node >/dev/null 2>&1; then node --check "$ROOT/app/static/app.js"; fi

echo '[3/8] Freezing Python, PDF engine and WebKit bridge into .app...'
ARGS=(--noconfirm --clean --onedir --windowed
    --name "$APP_NAME"
    --osx-bundle-identifier "$APP_ID"
    --icon "$ICON"
    --add-data "$ROOT/app/static:static"
    --collect-data webview
    --hidden-import webview.platforms.cocoa
    --hidden-import objc
    --hidden-import AppKit
    --hidden-import Foundation
    --hidden-import Cocoa
    --hidden-import WebKit
    --hidden-import Quartz
    --hidden-import Security
    --hidden-import UniformTypeIdentifiers
    --distpath "$BUILD/dist"
    --workpath "$BUILD/work"
    --specpath "$BUILD/spec")
if [[ -n "$APP_SIGN" ]]; then
    ARGS+=(--codesign-identity "$APP_SIGN" --osx-entitlements-file "$ROOT/macos/entitlements.plist")
fi
"$PY" -m PyInstaller "${ARGS[@]}" "$APP_ENTRY"
BUNDLE="$BUILD/dist/$APP_NAME.app"
if [[ ! -d "$BUNDLE/Contents/MacOS" ]]; then
    echo '[ERROR] PyInstaller did not create a Finder .app bundle.' >&2
    exit 3
fi

echo '[4/8] Applying app metadata, icon and verifying code signature...'
"$PY" "$ROOT/macos/stamp_plist.py" "$BUNDLE/Contents/Info.plist" "$VERSION" "$APP_ID"
if [[ -n "$APP_SIGN" ]]; then
    codesign --force --sign "$APP_SIGN" --options runtime \
        --entitlements "$ROOT/macos/entitlements.plist" "$BUNDLE"
else
    codesign --force --sign - "$BUNDLE"
fi
codesign --verify --deep --strict --verbose=2 "$BUNDLE"

echo '[5/8] Verifying frozen app without installed Python (clean PATH)...'
check_bundled_runtime

echo '[6/8] Notarizing and stapling the APPLICATION when requested...'
if [[ "$NOTARIZE" == 1 ]]; then
    # Notarize the real app first, so the directly-downloadable App.zip also
    # contains a stapled app and passes Gatekeeper (not only the .pkg/.dmg).
    INITIAL_ZIP="$BUILD/notary-submission-App.zip"
    rm -f "$INITIAL_ZIP"
    ditto -c -k --sequesterRsrc --keepParent "$BUNDLE" "$INITIAL_ZIP"
    notarize_file "$INITIAL_ZIP"
    xcrun stapler staple "$BUNDLE"
    xcrun stapler validate "$BUNDLE"
    codesign --verify --deep --strict --verbose=2 "$BUNDLE"
else
    echo '  Apple notarization not enabled: this app is for internal testing.'
fi

PKG="$OUT/PaperIsles-$VERSION-macOS-$ARCH-$CHANNEL.pkg"
DMG="$OUT/PaperIsles-$VERSION-macOS-$ARCH-$CHANNEL.dmg"
APP_ZIP="$OUT/PaperIsles-$VERSION-macOS-$ARCH-$CHANNEL-App.zip"
STAGING="$BUILD/dmg-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
ditto "$BUNDLE" "$STAGING/$APP_NAME.app"
ln -s /Applications "$STAGING/Applications"
printf '纸屿 Paper Isles / INSTALL\n\n将 纸屿 Paper Isles.app 拖入 Applications（应用程序），然后双击打开。\n也可直接双击 .pkg 按系统安装向导安装。\n无需 Python、pip、Terminal 或安装依赖脚本。\n' > "$STAGING/安装说明.txt"

echo '[7/8] Producing standalone .app.zip, graphical .pkg and .dmg...'
rm -f "$APP_ZIP" "$PKG" "$DMG"
ditto -c -k --sequesterRsrc --keepParent "$BUNDLE" "$APP_ZIP"
# PKG is the recommended single-file download for nontechnical users.
# A component package is required for the postinstall desktop shortcut hook.
PKG_ROOT="$BUILD/pkg-root"
PKG_SCRIPTS="$BUILD/pkg-scripts"
COMPONENT_PKG="$BUILD/PaperIsles-component.pkg"
rm -rf "$PKG_ROOT" "$PKG_SCRIPTS" "$COMPONENT_PKG"
mkdir -p "$PKG_ROOT/Applications" "$PKG_SCRIPTS"
ditto "$BUNDLE" "$PKG_ROOT/Applications/$APP_NAME.app"
sed "s|__APP_NAME__|$APP_NAME|g" "$ROOT/macos/pkg-scripts/postinstall" > "$PKG_SCRIPTS/postinstall"
chmod 755 "$PKG_SCRIPTS/postinstall"
pkgbuild --root "$PKG_ROOT" \
    --identifier "$APP_ID.install" --version "$VERSION" \
    --install-location / --scripts "$PKG_SCRIPTS" "$COMPONENT_PKG"
if [[ -n "$INSTALLER_SIGN" ]]; then
    productbuild --package "$COMPONENT_PKG" --sign "$INSTALLER_SIGN" "$PKG"
    pkgutil --check-signature "$PKG"
else
    productbuild --package "$COMPONENT_PKG" "$PKG"
fi
if ! pkgutil --payload-files "$PKG" | grep -F "Applications/$APP_NAME.app/Contents/Info.plist" >/dev/null; then
    echo '[ERROR] Installer does not contain the expected .app bundle.' >&2
    exit 3
fi
# Opt-in CI smoke test: actually install the PKG on the disposable macOS runner.
if [[ "${PAPER_ISLES_INSTALL_VERIFY:-0}" == 1 ]]; then
    sudo /usr/sbin/installer -pkg "$PKG" -target /
    if [[ ! -x "/Applications/$APP_NAME.app/Contents/MacOS/$APP_NAME" ]]; then
        echo '[ERROR] PKG did not install a launchable .app to Applications.' >&2
        exit 3
    fi
    check_bundled_runtime "/Applications/$APP_NAME.app"
fi
hdiutil create -volname 'Paper Isles' -srcfolder "$STAGING" -format UDZO -ov "$DMG"

# Verify the ARCHIVE is an actual app, not merely a source-code zip. Preserve
# resource forks, executable bits and symlinks using Apple's ditto utility.
ZIP_VERIFY="$BUILD/verify-appzip"
rm -rf "$ZIP_VERIFY"
mkdir -p "$ZIP_VERIFY"
ditto -x -k "$APP_ZIP" "$ZIP_VERIFY"
if [[ ! -x "$ZIP_VERIFY/$APP_NAME.app/Contents/MacOS/$APP_NAME" ]]; then
    echo '[ERROR] Downloadable app ZIP lost its macOS launch executable.' >&2
    exit 3
fi
codesign --verify --deep --strict --verbose=2 "$ZIP_VERIFY/$APP_NAME.app"
check_bundled_runtime "$ZIP_VERIFY/$APP_NAME.app"
rm -rf "$ZIP_VERIFY"

echo '[8/8] Notarizing and stapling installers when requested...'
if [[ "$NOTARIZE" == 1 ]]; then
    for FILE in "$PKG" "$DMG"; do
        notarize_file "$FILE"
        xcrun stapler staple "$FILE"
        xcrun stapler validate "$FILE"
    done
    spctl --assess --verbose=2 --type execute "$BUNDLE"
else
    echo '  Developer/unnotarized packages are not for general public distribution.'
fi

cat <<EOF
------------------------------------------------------------------
[OK] Self-contained Finder app:  $BUNDLE
[OK] App download ZIP:         $APP_ZIP
[OK] Recommended Mac Installer: $PKG
[OK] Mac drag-install DMG:     $DMG
[OK] Architecture:             $ARCH
[OK] Distribution status:      $CHANNEL

End users double-click the PKG, approve the installation, then use its desktop icon.
NO Python, pip, developer tools, dependency scripts or Terminal required.
EOF
