import unittest
from unittest.mock import patch, MagicMock, ANY
import os
import io
import sys
import tempfile
import warnings

from orpheus.download_backend import (
    DownloadBackend,
    NativeBackend,
    Aria2cBackend,
    build_aria2c_args,
    resolve_downloader_settings,
    get_backend,
    get_global_downloader_settings,
    set_global_downloader_settings,
    parse_bytes,
    create_progress_bar,
    ARIA2C_PROGRESS_RE
)
from utils.utils import download_file


class TestArgumentBuilder(unittest.TestCase):
    """Unit tests for the aria2c argument builder."""

    def test_default_argument_builder(self):
        args = build_aria2c_args(
            binary_path='aria2c',
            url='https://example.com/audio.flac',
            file_location='/downloads/Artist/Album/track.flac'
        )
        self.assertIsInstance(args, list)
        self.assertEqual(args[0], 'aria2c')
        self.assertEqual(args[-1], 'https://example.com/audio.flac')

        # Check directory and output
        self.assertIn('--dir=/downloads/Artist/Album', args)
        self.assertIn('--out=track.flac', args)

        # Check default performance tuning
        self.assertIn('--max-connection-per-server=4', args)
        self.assertIn('--split=4', args)
        self.assertIn('--min-split-size=1M', args)

        # Check resume and overwrite options
        self.assertIn('--continue=true', args)
        self.assertIn('--allow-overwrite=true', args)
        self.assertIn('--auto-file-renaming=false', args)

        # Check quiet defaults when progress bar is disabled
        self.assertIn('--quiet=true', args)
        self.assertIn('--summary-interval=0', args)

    def test_custom_options(self):
        backend = Aria2cBackend(
            binary_path='/usr/local/bin/aria2c',
            max_connections=16,
            split=8,
            min_split_size='2M',
            continue_download=False,
            extra_args=['--timeout=30', '--retry-wait=2']
        )
        args = backend.build_args(
            url='https://example.com/music.mp3',
            file_location='music.mp3',
            enable_progress_bar=True
        )
        self.assertEqual(args[0], '/usr/local/bin/aria2c')
        self.assertIn('--dir=.', args)
        self.assertIn('--out=music.mp3', args)
        self.assertIn('--max-connection-per-server=16', args)
        self.assertIn('--split=8', args)
        self.assertIn('--min-split-size=2M', args)
        self.assertIn('--continue=false', args)
        self.assertIn('--timeout=30', args)
        self.assertIn('--retry-wait=2', args)

        # When progress bar is enabled
        self.assertIn('--summary-interval=1', args)
        self.assertIn('--human-readable=false', args)
        self.assertIn('--show-console-readout=true', args)
        self.assertNotIn('--quiet=true', args)

    def test_headers_serialization(self):
        headers = {
            'Authorization': 'Bearer test-token-12345',
            'User-Agent': 'OrpheusDL/UnitTests',
            'X-Custom-Header': 'CustomValue'
        }
        args = build_aria2c_args(
            binary_path='aria2c',
            url='https://example.com/file',
            file_location='/tmp/file',
            headers=headers
        )
        self.assertIn('--header=Authorization: Bearer test-token-12345', args)
        self.assertIn('--header=User-Agent: OrpheusDL/UnitTests', args)
        self.assertIn('--header=X-Custom-Header: CustomValue', args)

    def test_argument_safety_and_spaces(self):
        url = 'https://example.com/stream?id=1&token=abc#frag'
        file_loc = '/home/user/Music/Artist Name [2026]/01 - Special "Track" ($).flac'
        args = build_aria2c_args(
            binary_path='aria2c',
            url=url,
            file_location=file_loc
        )
        # Verify that arguments are safely separated into distinct argv elements
        self.assertIn('--dir=/home/user/Music/Artist Name [2026]', args)
        self.assertIn('--out=01 - Special "Track" ($).flac', args)
        self.assertEqual(args[-1], url)
        # All items must be strings
        for item in args:
            self.assertIsInstance(item, str)

    def test_no_overwrite_flag_when_continue_false(self):
        args = build_aria2c_args(
            binary_path='aria2c',
            url='https://example.com/audio.flac',
            file_location='/downloads/track.flac',
            continue_download=False
        )
        self.assertNotIn('--allow-overwrite=true', args)
        self.assertIn('--auto-file-renaming=false', args)
        self.assertIn('--continue=false', args)


class TestFallbackPath(unittest.TestCase):
    """Unit tests for the fallback path from Aria2cBackend to NativeBackend."""

    def test_fallback_when_binary_missing(self):
        mock_native = MagicMock(spec=NativeBackend)
        mock_native.download.return_value = True

        backend = Aria2cBackend(
            binary_path='/nonexistent/path/to/aria2c_binary_xyz',
            fallback_backend=mock_native
        )

        with warnings.catch_warnings(record=True) as captured_warnings:
            warnings.simplefilter('always')
            res = backend.download(
                url='https://example.com/song.flac',
                file_location='/tmp/test_fallback.flac',
                headers={'X-Auth': 'token'},
                enable_progress_bar=False,
                indent_level=2
            )

        self.assertTrue(res)
        # Verify fallback backend was invoked with identical parameters
        mock_native.download.assert_called_once_with(
            'https://example.com/song.flac',
            '/tmp/test_fallback.flac',
            headers={'X-Auth': 'token'},
            enable_progress_bar=False,
            indent_level=2,
            artwork_settings=None,
            continue_download=False
        )

        # Verify a warning was emitted
        self.assertTrue(any('aria2c failure' in str(w.message) for w in captured_warnings))

    @patch('shutil.which', return_value='/usr/bin/aria2c')
    @patch('os.path.isfile', return_value=True)
    @patch('os.access', return_value=True)
    @patch('subprocess.run')
    def test_fallback_when_aria2c_exits_nonzero(self, mock_subproc_run, mock_access, mock_isfile, mock_which):
        mock_native = MagicMock(spec=NativeBackend)
        mock_native.download.return_value = True

        # Simulate aria2c process failing with exit code 1
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_subproc_run.return_value = mock_proc

        backend = Aria2cBackend(
            binary_path='aria2c',
            fallback_backend=mock_native
        )

        with warnings.catch_warnings(record=True) as captured_warnings:
            warnings.simplefilter('always')
            res = backend.download(
                url='https://example.com/track.flac',
                file_location='/tmp/test_nonzero.flac',
                enable_progress_bar=False
            )

        self.assertTrue(res)
        mock_native.download.assert_called_once()
        self.assertTrue(any('exited with return code 1' in str(w.message) for w in captured_warnings))

    @patch('shutil.which', return_value='/usr/bin/aria2c')
    @patch('os.path.isfile', return_value=True)
    @patch('os.access', return_value=True)
    @patch('subprocess.Popen', side_effect=FileNotFoundError('aria2c executable not found'))
    def test_fallback_on_filenotfound_exception(self, mock_popen, mock_access, mock_isfile, mock_which):
        mock_native = MagicMock(spec=NativeBackend)
        mock_native.download.return_value = True

        backend = Aria2cBackend(
            binary_path='aria2c',
            fallback_backend=mock_native
        )

        with warnings.catch_warnings(record=True) as captured_warnings:
            warnings.simplefilter('always')
            res = backend.download(
                url='https://example.com/track.flac',
                file_location='/tmp/test_fnf.flac',
                enable_progress_bar=True
            )

        self.assertTrue(res)
        mock_native.download.assert_called_once()
        self.assertTrue(any('aria2c failure' in str(w.message) for w in captured_warnings))

    @patch('shutil.which', return_value='/usr/bin/aria2c')
    @patch('os.path.isfile', return_value=True)
    @patch('os.access', return_value=True)
    @patch('subprocess.run')
    def test_cleanup_stale_aria2_file_on_fallback(self, mock_subproc_run, mock_access, mock_isfile, mock_which):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_loc = os.path.join(tmpdir, 'sample.flac')
            aria2_file = file_loc + '.aria2'
            # Create a partial output file and a stale control file
            with open(file_loc, 'wb') as f:
                f.write(b'partial corrupted data')
            with open(aria2_file, 'w') as f:
                f.write('control data')

            mock_native = MagicMock(spec=NativeBackend)
            mock_native.download.return_value = True

            mock_proc = MagicMock()
            mock_proc.returncode = 22
            mock_subproc_run.return_value = mock_proc

            backend = Aria2cBackend(
                binary_path='aria2c',
                fallback_backend=mock_native
            )

            backend.download(
                url='https://example.com/sample.flac',
                file_location=file_loc,
                enable_progress_bar=False
            )

            self.assertFalse(os.path.exists(file_loc))
            self.assertFalse(os.path.exists(aria2_file))

    @patch('shutil.which', return_value='/usr/bin/aria2c')
    @patch('subprocess.run')
    def test_fallback_deletes_partial_and_control_file(self, mock_subproc_run, mock_which):
        """aria2c failure must not hand a corrupt partial to NativeBackend."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "track.flac")
            ctrl = dest + ".aria2"
            # Simulate out-of-order partial + control file from a failed aria2c run
            with open(dest, "wb") as f:
                f.write(b"\x00" * 1024)
            with open(ctrl, "w") as f:
                f.write("[aria2 control stub]")

            mock_native = MagicMock(spec=NativeBackend)
            mock_native.download.return_value = True

            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_subproc_run.return_value = mock_proc

            backend = Aria2cBackend(
                settings={"continue": True, "split": 4},
                fallback_backend=mock_native
            )
            backend.download("http://example.com/song.flac", dest, enable_progress_bar=False)

            self.assertFalse(os.path.exists(dest), "partial file must be deleted")
            self.assertFalse(os.path.exists(ctrl), ".aria2 must be deleted")
            # Confirm NativeBackend was told to start fresh
            _, call_kwargs = mock_native.download.call_args
            self.assertFalse(call_kwargs.get("continue_download"))

    @patch('shutil.which', return_value='/usr/bin/aria2c')
    @patch('subprocess.run')
    def test_fallback_does_not_mutate_caller_kwargs_and_works_with_real_native(self, mock_subproc_run, mock_which):
        """Confirm kwargs passed to _fallback is not mutated, and real NativeBackend accepts fallback kwargs."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "track.flac")
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_subproc_run.return_value = mock_proc

            real_native = NativeBackend()
            backend = Aria2cBackend(fallback_backend=real_native)

            # Pass a custom kwargs dict
            caller_kwargs = {'indent_level': 2, 'enable_progress_bar': False}
            caller_kwargs_snapshot = dict(caller_kwargs)

            with patch.object(real_native, 'download', wraps=real_native.download) as spy_native_download:
                with patch('orpheus.download_backend.backend_session.get') as mock_get:
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.headers = {'content-length': '5'}
                    mock_resp.iter_content.return_value = [b'hello']
                    mock_get.return_value = mock_resp

                    backend._fallback("test failure", "http://example.com/test", dest, **caller_kwargs)

            # 1. Caller kwargs must not be mutated
            self.assertEqual(caller_kwargs, caller_kwargs_snapshot)
            self.assertNotIn('continue_download', caller_kwargs)

            # 2. Real NativeBackend.download was called with continue_download=False without TypeError
            spy_native_download.assert_called_once()
            _, kwargs_passed = spy_native_download.call_args
            self.assertFalse(kwargs_passed.get('continue_download'))


class TestSettingsResolution(unittest.TestCase):
    """Unit tests for resolving configuration settings in various formats."""

    def test_default_resolution(self):
        resolved = resolve_downloader_settings({})
        self.assertEqual(resolved['backend'], 'native')
        self.assertEqual(resolved['binary_path'], 'aria2c')
        self.assertEqual(resolved['max_connections'], 4)
        self.assertEqual(resolved['split'], 4)
        self.assertEqual(resolved['min_split_size'], '1M')
        self.assertTrue(resolved['continue'])
        self.assertEqual(resolved['extra_args'], [])

    def test_nested_format(self):
        settings = {
            'backend': 'aria2c',
            'aria2c': {
                'binary_path': '/custom/aria2c',
                'max_connections': 8,
                'split': 6,
                'min_split_size': '5M',
                'continue': False,
                'extra_args': ['--log=aria2.log']
            }
        }
        resolved = resolve_downloader_settings(settings)
        self.assertEqual(resolved['backend'], 'aria2c')
        self.assertEqual(resolved['binary_path'], '/custom/aria2c')
        self.assertEqual(resolved['max_connections'], 8)
        self.assertEqual(resolved['split'], 6)
        self.assertEqual(resolved['min_split_size'], '5M')
        self.assertFalse(resolved['continue'])
        self.assertEqual(resolved['extra_args'], ['--log=aria2.log'])

    def test_dotted_notation(self):
        settings = {
            'backend': 'aria2c',
            'aria2c.binary_path': '/usr/bin/custom_aria2c',
            'max_connections': 12,
            'split': 12,
            'min_split_size': '10M',
            'continue': True,
            'extra_args': ['--dry-run=true']
        }
        resolved = resolve_downloader_settings(settings)
        self.assertEqual(resolved['backend'], 'aria2c')
        self.assertEqual(resolved['binary_path'], '/usr/bin/custom_aria2c')
        self.assertEqual(resolved['max_connections'], 12)
        self.assertEqual(resolved['split'], 12)
        self.assertEqual(resolved['min_split_size'], '10M')
        self.assertTrue(resolved['continue'])
        self.assertEqual(resolved['extra_args'], ['--dry-run=true'])

    def test_get_backend_factory(self):
        backend_native = get_backend('native')
        self.assertIsInstance(backend_native, NativeBackend)

        backend_aria = get_backend('aria2c')
        self.assertIsInstance(backend_aria, Aria2cBackend)


class TestUnifiedProgressAndParsers(unittest.TestCase):
    """Unit tests for progress reporting helpers and byte parsers."""

    def test_parse_bytes(self):
        self.assertEqual(parse_bytes('0B'), 0)
        self.assertEqual(parse_bytes('16384B'), 16384)
        self.assertEqual(parse_bytes('1024'), 1024)
        self.assertEqual(parse_bytes(2048), 2048)
        self.assertEqual(parse_bytes('1KiB'), 1024)
        self.assertEqual(parse_bytes('1MiB'), 1024 * 1024)
        self.assertEqual(parse_bytes('1.5MiB'), int(1.5 * 1024 * 1024))
        self.assertEqual(parse_bytes('1GiB'), 1024 * 1024 * 1024)
        self.assertIsNone(parse_bytes('invalid_size'))
        self.assertIsNone(parse_bytes(None))

    def test_create_progress_bar(self):
        bar = create_progress_bar(total=10240, indent_level=2, initial=0)
        self.assertIsNotNone(bar)
        bar.update(1024)
        self.assertEqual(bar.n, 1024)
        bar.close()

    def test_progress_regex_matches_aria2c_1_37_output(self):
        line = "[#1a2b3c 12MiB/25MiB(48%) CN:4 DL:8.3MiB ETA:1s]"
        m = ARIA2C_PROGRESS_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "12MiB")
        self.assertEqual(m.group(2), "25MiB")
        self.assertEqual(m.group(3), "48")


class TestNativeBackendResume(unittest.TestCase):
    """Unit tests for NativeBackend range requests and resume support."""

    @patch('orpheus.download_backend.backend_session.get')
    def test_native_resume_http_206(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_loc = os.path.join(tmpdir, 'resume_test.dat')
            # Write 500 initial bytes
            with open(file_loc, 'wb') as f:
                f.write(b'A' * 500)

            # Mock 206 Partial Content response
            mock_resp = MagicMock()
            mock_resp.status_code = 206
            mock_resp.headers = {'content-length': '500'}
            mock_resp.iter_content.return_value = [b'B' * 500]
            mock_get.return_value = mock_resp

            backend = NativeBackend(continue_download=True)
            res = backend.download('https://example.com/file', file_loc)

            self.assertTrue(res)
            # Check Range header was sent
            mock_get.assert_called_once()
            called_headers = mock_get.call_args[1].get('headers', {})
            self.assertEqual(called_headers.get('Range'), 'bytes=500-')

            # Verify file now contains 1000 bytes
            with open(file_loc, 'rb') as f:
                data = f.read()
            self.assertEqual(len(data), 1000)
            self.assertEqual(data[:500], b'A' * 500)
            self.assertEqual(data[500:], b'B' * 500)

    @patch('orpheus.download_backend.backend_session.get')
    def test_native_resume_http_416_complete(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_loc = os.path.join(tmpdir, 'complete_test.dat')
            with open(file_loc, 'wb') as f:
                f.write(b'X' * 1000)

            # Mock 416 Range Not Satisfiable (already finished)
            mock_resp = MagicMock()
            mock_resp.status_code = 416
            mock_get.return_value = mock_resp

            backend = NativeBackend(continue_download=True)
            res = backend.download('https://example.com/file', file_loc)

            self.assertTrue(res)
            # Verify file unchanged
            self.assertEqual(os.path.getsize(file_loc), 1000)


class TestDownloadFileRouting(unittest.TestCase):
    """Unit tests for the top-level download_file function routing."""

    @patch('orpheus.download_backend.Aria2cBackend.download', return_value=True)
    def test_download_file_routes_to_aria2c(self, mock_aria_download):
        settings = {'backend': 'aria2c'}
        res = download_file(
            url='https://example.com/test.flac',
            file_location='/tmp/routed_aria.flac',
            downloader_settings=settings
        )
        self.assertTrue(res)
        mock_aria_download.assert_called_once()

    @patch('orpheus.download_backend.NativeBackend.download', return_value=True)
    def test_download_file_routes_to_native(self, mock_native_download):
        settings = {'backend': 'native'}
        res = download_file(
            url='https://example.com/test.flac',
            file_location='/tmp/routed_native.flac',
            downloader_settings=settings
        )
        self.assertTrue(res)
        mock_native_download.assert_called_once()

    @patch('orpheus.download_backend.Aria2cBackend.download', return_value=True)
    def test_download_file_downloader_settings_override(self, mock_aria_download):
        settings = {'downloader': {'backend': 'aria2c', 'max_connections': 8}}
        res = download_file(
            url='https://example.com/test.flac',
            file_location='/tmp/routed_override.flac',
            downloader_settings=settings
        )
        self.assertTrue(res)
        mock_aria_download.assert_called_once()

    @patch('orpheus.download_backend.Aria2cBackend.download', return_value=True)
    def test_download_file_uses_global_settings_fallback_when_none_provided(self, mock_aria_download):
        """When downloader_settings=None, download_file falls back to global settings."""
        try:
            set_global_downloader_settings({'backend': 'aria2c'})
            res = download_file(
                url='https://example.com/test.flac',
                file_location='/tmp/routed_global.flac',
                downloader_settings=None
            )
            self.assertTrue(res)
            mock_aria_download.assert_called_once()
        finally:
            set_global_downloader_settings(None)

    def test_module_boundary_decoupling_verification(self):
        """Verify that DeezerAPI and ModuleController do not expose downloader settings."""
        from modules.deezer.dzapi import DeezerAPI
        from utils.models import ModuleController
        import inspect

        # 1. DeezerAPI constructor and instance attributes do not have downloader_settings
        sig = inspect.signature(DeezerAPI.__init__)
        self.assertNotIn('downloader_settings', sig.parameters)
        self.assertNotIn('settings', sig.parameters)
        api = DeezerAPI(Exception, 'client_id', 'client_secret', 'bf_secret')
        self.assertFalse(hasattr(api, 'downloader_settings'))

        # 2. ModuleController dataclass does not have downloader_settings
        self.assertNotIn('downloader_settings', ModuleController.__annotations__)


class TestInterruptHandling(unittest.TestCase):
    """Unit tests for interrupt handling in download backends."""

    @patch('orpheus.download_backend.backend_session.get')
    def test_native_interrupt_deletes_when_continue_false(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_loc = os.path.join(tmpdir, 'interrupt_false.dat')

            def raise_interrupt(*args, **kwargs):
                # Write some partial data
                with open(file_loc, 'wb') as f:
                    f.write(b'partial')
                raise KeyboardInterrupt()

            mock_get.side_effect = raise_interrupt
            backend = NativeBackend(continue_download=False)
            with self.assertRaises(KeyboardInterrupt):
                backend.download('https://example.com/file', file_loc)

            # File should be removed when continue_download is False
            self.assertFalse(os.path.exists(file_loc))

    @patch('orpheus.download_backend.backend_session.get')
    def test_native_interrupt_preserves_when_continue_true(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_loc = os.path.join(tmpdir, 'interrupt_true.dat')

            def raise_interrupt(*args, **kwargs):
                with open(file_loc, 'wb') as f:
                    f.write(b'partial')
                raise KeyboardInterrupt()

            mock_get.side_effect = raise_interrupt
            backend = NativeBackend(continue_download=True)
            with self.assertRaises(KeyboardInterrupt):
                backend.download('https://example.com/file', file_loc)

            # File should be preserved for resumption when continue_download is True
            self.assertTrue(os.path.exists(file_loc))


class TestArtworkResize(unittest.TestCase):
    """Unit tests for post-download artwork processing."""

    def test_artwork_resize(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, 'cover.jpg')
            # Create a 200x200 dummy image
            img = Image.new('RGB', (200, 200), color='red')
            img.save(img_path, 'JPEG')

            from orpheus.download_backend import resize_artwork
            resize_artwork(img_path, {
                'should_resize': True,
                'resolution': 100,
                'format': 'jpeg',
                'compression': 'high'
            })

            with Image.open(img_path) as resized:
                self.assertEqual(resized.size, (100, 100))


class TestRealAria2cExecution(unittest.TestCase):
    """Integration test using a local HTTP server with Range support and real aria2c."""

    def setUp(self):
        import http.server, socketserver, threading
        self.test_data = b'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' * 1000

        test_data = self.test_data
        class RangeHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                range_hdr = self.headers.get('Range')
                if range_hdr and range_hdr.startswith('bytes='):
                    start = int(range_hdr.split('bytes=')[1].split('-')[0])
                    if start >= len(test_data):
                        self.send_response(416)
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Range', f'bytes {start}-{len(test_data)-1}/{len(test_data)}')
                    self.send_header('Content-Length', str(len(test_data) - start))
                    self.end_headers()
                    self.wfile.write(test_data[start:])
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(test_data)))
                    self.end_headers()
                    self.wfile.write(test_data)

            def log_message(self, format, *args):
                pass

        self.server = socketserver.TCPServer(('127.0.0.1', 0), RangeHandler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()
        self.url = f'http://127.0.0.1:{self.port}/media.bin'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_real_aria2c_download(self):
        import shutil
        if not shutil.which('aria2c'):
            self.skipTest('aria2c executable not available in environment')

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, 'downloaded.bin')
            backend = Aria2cBackend(binary_path='aria2c', continue_download=True)
            res = backend.download(self.url, dest, enable_progress_bar=False)

            self.assertTrue(res)
            self.assertTrue(os.path.isfile(dest))
            with open(dest, 'rb') as f:
                self.assertEqual(f.read(), self.test_data)

    def test_real_aria2c_resume(self):
        import shutil
        if not shutil.which('aria2c'):
            self.skipTest('aria2c executable not available in environment')

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, 'resumed.bin')
            # Pre-write 500 bytes
            with open(dest, 'wb') as f:
                f.write(self.test_data[:500])

            backend = Aria2cBackend(binary_path='aria2c', continue_download=True)
            res = backend.download(self.url, dest, enable_progress_bar=False)

            self.assertTrue(res)
            with open(dest, 'rb') as f:
                self.assertEqual(f.read(), self.test_data)

    def test_real_aria2c_fallback_to_native(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, 'fallback_dest.bin')
            # Point to non-existent aria2c binary, should warn and succeed via NativeBackend
            backend = Aria2cBackend(binary_path='missing_aria2c_binary_12345')

            with warnings.catch_warnings(record=True) as captured_warnings:
                warnings.simplefilter('always')
                res = backend.download(self.url, dest, enable_progress_bar=False)

            self.assertTrue(res)
            self.assertTrue(os.path.isfile(dest))
            with open(dest, 'rb') as f:
                self.assertEqual(f.read(), self.test_data)
            self.assertTrue(any('binary not found' in str(w.message) for w in captured_warnings))

    def test_real_aria2c_download_with_progress_bar(self):
        """Integration test: real aria2c with --console-log-level=warn emits progress and downloads cleanly."""
        import shutil
        if not shutil.which('aria2c'):
            self.skipTest('aria2c executable not available in environment')

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, 'downloaded_progress.bin')
            backend = Aria2cBackend(binary_path='aria2c')
            res = backend.download(self.url, dest, enable_progress_bar=True)

            self.assertTrue(res)
            self.assertTrue(os.path.isfile(dest))
            with open(dest, 'rb') as f:
                self.assertEqual(f.read(), self.test_data)

    def test_real_aria2c_fallback_cleans_corrupt_partial_and_recovers_cleanly(self):
        """Integration test: corrupt partial + .aria2 cleaned up on failure, NativeBackend downloads fresh."""
        import shutil
        if not shutil.which('aria2c'):
            self.skipTest('aria2c executable not available in environment')

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, 'corrupt_track.bin')
            ctrl = dest + '.aria2'

            # 1. Write an out-of-order/corrupt partial file and .aria2 control file
            with open(dest, 'wb') as f:
                f.write(b'GARBAGE_CHUNK_FROM_FAILED_ARIA2C' * 50)
            with open(ctrl, 'w') as f:
                f.write('aria2-control-stub')

            self.assertTrue(os.path.exists(dest))
            self.assertTrue(os.path.exists(ctrl))

            # 2. Configure Aria2cBackend with an invalid extra arg to trigger aria2c failure exit
            backend = Aria2cBackend(
                binary_path='aria2c',
                extra_args=['--invalid-flag-that-triggers-aria2c-failure'],
                fallback_backend=NativeBackend()
            )

            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                res = backend.download(self.url, dest, enable_progress_bar=False)

            self.assertTrue(res)
            # 3. Control file must be removed
            self.assertFalse(os.path.exists(ctrl), ".aria2 control file must be deleted")
            # 4. Resulting file must match expected data completely without corruption
            with open(dest, 'rb') as f:
                self.assertEqual(f.read(), self.test_data)

    def test_real_aria2c_auto_file_renaming_false_prevents_silent_duplicates(self):
        """Integration test: when continue_download=False and file exists, aria2c errors instead of creating .1 file."""
        import shutil
        if not shutil.which('aria2c'):
            self.skipTest('aria2c executable not available in environment')

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, 'existing.bin')
            with open(dest, 'wb') as f:
                f.write(b'existing_content')

            # continue_download=False means no overwrite flag, but auto-file-renaming=false is present
            backend = Aria2cBackend(binary_path='aria2c', continue_download=False, fallback_backend=NativeBackend(continue_download=False))
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                res = backend.download(self.url, dest, enable_progress_bar=False)

            # Fallback NativeBackend overwrites the file cleanly
            self.assertTrue(res)
            # Check that no duplicate file (e.g. existing.1.bin) was created by aria2c
            dir_contents = os.listdir(tmpdir)
            self.assertEqual(dir_contents, ['existing.bin'], f"Unexpected files created: {dir_contents}")
            with open(dest, 'rb') as f:
                self.assertEqual(f.read(), self.test_data)


if __name__ == '__main__':
    unittest.main()
