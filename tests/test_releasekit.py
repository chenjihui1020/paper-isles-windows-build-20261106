"""Static and isolated cross-platform release engineering tests."""
from pathlib import Path
import importlib.util
import plistlib
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseKitTests(unittest.TestCase):
    def test_layout_and_original_logo(self):
        self.assertEqual((ROOT / 'VERSION').read_text().strip(), '0.4.2')
        for item in (
            'app/app.py', 'app/pdf_text_edit.py', 'app/static/index.html',
            'app/static/app.js', 'app/static/style.css',
            'assets/PaperIsles.icns', 'assets/PaperIsles-icon-source.png',
            'macos/build_macos.sh', 'macos/pkg-scripts/postinstall', '.github/workflows/macos-installers.yml',
        ):
            self.assertTrue((ROOT / item).is_file(), item)
        self.assertGreater((ROOT / 'assets/PaperIsles.icns').stat().st_size, 100000)

    def test_finder_metadata_and_icon_from_uploaded_image(self):
        loader = importlib.util.spec_from_file_location('stamp_plist', ROOT / 'macos/stamp_plist.py')
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / 'Info.plist'
            plist.write_bytes(plistlib.dumps({
                'CFBundleExecutable': 'Paper Isles',
                'CFBundleIconFile': 'PaperIsles.icns',
            }))
            module.stamp(plist, '0.4.2', 'app.paperisles.localdesktop')
            result = plistlib.loads(plist.read_bytes())
            self.assertEqual(result['CFBundleDisplayName'], '纸屿 · Paper Isles')
            self.assertEqual(result['CFBundleExecutable'], 'Paper Isles')
            self.assertEqual(result['CFBundleIconFile'], 'PaperIsles.icns')
            self.assertEqual(result['CFBundleShortVersionString'], '0.4.2')
            self.assertEqual(result['CFBundleVersion'], '402')
            self.assertEqual(result['LSMinimumSystemVersion'], '13.0')

    def test_pkg_dmg_frozen_runtime_and_notarization_routes(self):
        builder = (ROOT / 'macos/build_macos.sh').read_text('utf-8')
        for item in (
            '--windowed', '--onedir', '--icon "$ICON"',
            'pkgbuild --root', 'productbuild --package', 'hdiutil create', 'PAPER_ISLES_HOME',
            'App.zip', 'ditto -c -k --sequesterRsrc --keepParent',
            'env -i HOME=', 'check_bundled_runtime',
            '--self-check', 'codesign --verify',
            'notarytool submit', 'stapler staple',
            'Installer', 'DEVELOPMENT-UNNOTARIZED', 'macos/pkg-scripts/postinstall', 'PAPER_ISLES_INSTALL_VERIFY',
        ):
            self.assertIn(item, builder)
        workflow = (ROOT / '.github/workflows/macos-installers.yml').read_text('utf-8')
        self.assertIn('macos-15-intel', workflow)
        self.assertIn('macos-15', workflow)
        self.assertIn('upload-artifact', workflow)
        self.assertIn('App.zip', workflow)
        self.assertIn('test "${#assets[@]}" -eq 2', workflow)
        self.assertIn('PAPER_ISLES_NOTARY', workflow)
        self.assertIn('PAPER_ISLES_INSTALL_VERIFY:', workflow)
        self.assertNotIn('run: sudo spctl --master-disable', workflow)
        self.assertNotIn('xattr -dr com.apple.quarantine', builder)
        self.assertLess(builder.index('xcrun stapler staple "$BUNDLE"'), builder.index('ditto -c -k --sequesterRsrc --keepParent "$BUNDLE" "$APP_ZIP"'))

    @unittest.skipIf(sys.platform == 'win32', 'POSIX installer script test')
    def test_postinstall_desktop_link_is_safe_and_idempotent(self):
        script = ROOT / 'macos/pkg-scripts/postinstall'
        body = script.read_text(encoding='utf-8')
        self.assertIn('stat -f', body)
        self.assertIn('create_desktop_shortcut', body)
        self.assertIn('loginwindow', body)
        self.assertNotIn('sudo spctl', body)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            app = base / 'Applications' / 'Paper Isles.app'
            desktop = base / 'Desktop'
            app.mkdir(parents=True)
            desktop.mkdir()
            command = 'source "$1"; create_desktop_shortcut "$2" "$3" "Paper Isles.app"'
            args = ['bash', '-c', command, 'bash', str(script), str(desktop), str(app)]
            first = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            link = desktop / 'Paper Isles.app'
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), app.resolve())
            second = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            # Existing unrelated file must never be replaced by the installer.
            other = desktop / 'Keep This.app'
            other.write_text('custom shortcut', encoding='utf-8')
            no_overwrite = ['bash','-c', 'source "$1"; create_desktop_shortcut "$2" "$3" "Keep This.app"',
                            'bash', str(script), str(desktop), str(app)]
            status = subprocess.run(no_overwrite, capture_output=True, text=True)
            self.assertNotEqual(status.returncode, 0)
            self.assertEqual(other.read_text(encoding='utf-8'), 'custom shortcut')

    def test_release_builder_refuses_non_macos_packaging(self):
        if sys.platform != 'linux':
            self.skipTest('This check requires a non-macOS POSIX environment.')
        process = subprocess.run(
            ['bash', str(ROOT / 'macos/build_macos.sh')],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn('must be built on macOS', process.stderr)


if __name__ == '__main__':
    unittest.main()
