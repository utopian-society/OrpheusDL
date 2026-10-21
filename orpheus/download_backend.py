import os
import sys
import subprocess
import shutil
import re
import errno
import warnings
import json
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Union
from tqdm import tqdm
from PIL import Image
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def create_requests_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=10, backoff_factor=0.4, status_forcelist=[429, 500, 502, 503, 504])
    session.mount('http://', HTTPAdapter(max_retries=retries))
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


backend_session = create_requests_session()


def silentremove(filename: str) -> None:
    try:
        os.remove(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def parse_bytes(value_str: Union[str, int, float]) -> Optional[int]:
    """Parse byte quantities formatted as raw digits or human-readable strings like '1.0MiB' or '16384B'."""
    if value_str is None:
        return None
    if isinstance(value_str, (int, float)):
        return int(value_str)
    value_str = str(value_str).strip()
    match = re.match(r'^([0-9.]+)\s*([KMGT]?i?B)?$', value_str, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or '').upper()
    multipliers = {
        'B': 1, '': 1,
        'KIB': 1024, 'KB': 1000, 'K': 1024,
        'MIB': 1024**2, 'MB': 1000**2, 'M': 1024**2,
        'GIB': 1024**3, 'GB': 1000**3, 'G': 1024**3,
        'TIB': 1024**4, 'TB': 1000**4, 'T': 1024**4,
    }
    return int(number * multipliers.get(unit, 1))


def create_progress_bar(total: Optional[int], indent_level: int = 0, initial: int = 0) -> Optional[tqdm]:
    """Create a unified tqdm progress bar with indentation matching OrpheusDL output styling."""
    try:
        columns = os.get_terminal_size().columns
        ncols = (columns - indent_level) if columns > indent_level else None
    except Exception:
        ncols = None

    return tqdm(
        total=total,
        unit='B',
        unit_scale=True,
        unit_divisor=1024,
        initial=initial,
        miniters=1,
        ncols=ncols,
        bar_format=' ' * indent_level + '{l_bar}{bar}{r_bar}'
    )


def resize_artwork(file_location: str, artwork_settings: Optional[Dict[str, Any]] = None) -> None:
    if not (artwork_settings and artwork_settings.get('should_resize', False)):
        return
    if not os.path.isfile(file_location):
        return
    new_resolution = artwork_settings.get('resolution', 1400)
    new_format = artwork_settings.get('format', 'jpeg')
    if new_format == 'jpg':
        new_format = 'jpeg'
    new_compression = artwork_settings.get('compression', 'low')
    if new_compression == 'low':
        new_compression = 90
    elif new_compression == 'high':
        new_compression = 70
    if new_format == 'png':
        new_compression = None
    with Image.open(file_location) as im:
        im = im.resize((new_resolution, new_resolution), Image.Resampling.BICUBIC)
        im.save(file_location, new_format, quality=new_compression)


_GLOBAL_DOWNLOADER_SETTINGS: Optional[Dict[str, Any]] = None


def set_global_downloader_settings(settings: Optional[Dict[str, Any]]) -> None:
    global _GLOBAL_DOWNLOADER_SETTINGS
    _GLOBAL_DOWNLOADER_SETTINGS = settings


def get_global_downloader_settings() -> Dict[str, Any]:
    global _GLOBAL_DOWNLOADER_SETTINGS
    if _GLOBAL_DOWNLOADER_SETTINGS is not None:
        return _GLOBAL_DOWNLOADER_SETTINGS
    try:
        settings_path = os.path.join('config', 'settings.json')
        if os.path.isfile(settings_path):
            with open(settings_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('global', {}).get('downloader', {})
    except Exception:
        pass
    return {'backend': 'native'}


def resolve_downloader_settings(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Resolve and normalize downloader settings from arbitrary dictionaries,
    supporting nested, flat, and dotted formats (e.g. 'aria2c.binary_path' or aria2c.binary_path).
    """
    if settings is None:
        settings = get_global_downloader_settings()

    if isinstance(settings, dict):
        if 'global' in settings and isinstance(settings['global'], dict):
            settings = settings['global']
        if 'downloader' in settings and isinstance(settings['downloader'], dict):
            settings = settings['downloader']
    else:
        settings = {}

    backend = str(settings.get('backend', 'native')).lower().strip()
    aria2c_sub = settings.get('aria2c') if isinstance(settings.get('aria2c'), dict) else {}

    binary_path = (
        settings.get('aria2c.binary_path')
        or aria2c_sub.get('binary_path')
        or (settings.get('aria2c') if isinstance(settings.get('aria2c'), str) else None)
        or settings.get('binary_path')
        or 'aria2c'
    )

    max_connections = settings.get(
        'max_connections',
        aria2c_sub.get('max_connections', 4)
    )

    split = settings.get(
        'split',
        aria2c_sub.get('split', 4)
    )

    min_split_size = settings.get(
        'min_split_size',
        aria2c_sub.get('min_split_size', '1M')
    )

    if 'continue' in settings:
        continue_download = bool(settings['continue'])
    elif 'continue' in aria2c_sub:
        continue_download = bool(aria2c_sub['continue'])
    else:
        continue_download = True

    extra_args = settings.get(
        'extra_args',
        aria2c_sub.get('extra_args', [])
    )
    if extra_args is None:
        extra_args = []
    elif isinstance(extra_args, str):
        extra_args = [extra_args]
    else:
        extra_args = list(extra_args)

    return {
        'backend': backend,
        'binary_path': str(binary_path),
        'max_connections': int(max_connections),
        'split': int(split),
        'min_split_size': str(min_split_size),
        'continue': continue_download,
        'extra_args': extra_args,
        'aria2c': {
            'binary_path': str(binary_path),
            'max_connections': int(max_connections),
            'split': int(split),
            'min_split_size': str(min_split_size),
            'continue': continue_download,
            'extra_args': extra_args
        }
    }


def build_aria2c_args(
    binary_path: str,
    url: str,
    file_location: str,
    max_connections: int = 4,
    split: int = 4,
    min_split_size: str = '1M',
    continue_download: bool = True,
    extra_args: Optional[List[str]] = None,
    headers: Optional[Dict[str, str]] = None,
    enable_progress_bar: bool = False
) -> List[str]:
    """Safely build the argument list for aria2c execution without shell interpolation."""
    dir_path, file_name = os.path.split(file_location)
    if not dir_path:
        dir_path = '.'

    args = [
        str(binary_path or 'aria2c'),
        f'--dir={dir_path}',
        f'--out={file_name}',
        f'--max-connection-per-server={int(max_connections)}',
        f'--split={int(split)}',
        f'--min-split-size={str(min_split_size)}',
        '--auto-file-renaming=false',
    ]

    if continue_download:
        args.append('--allow-overwrite=true')
        args.append('--continue=true')
    else:
        args.append('--continue=false')

    if enable_progress_bar:
        args.extend([
            '--console-log-level=warn',
            '--summary-interval=1',
            '--human-readable=false',
            '--show-console-readout=true',
        ])
    else:
        args.extend([
            '--console-log-level=warn',
            '--quiet=true',
            '--summary-interval=0',
        ])

    if headers:
        for key, value in headers.items():
            args.append(f'--header={key}: {value}')

    if extra_args:
        args.extend(extra_args)

    args.append(url)
    return args


ARIA2C_PROGRESS_RE = re.compile(r'\[#[0-9a-fA-F]+\s+([0-9.]+[A-Za-z]*)/([0-9.]+[A-Za-z]*)\((\d+)%\)')


class DownloadBackend(ABC):
    """Abstract base class defining the download backend interface."""

    @abstractmethod
    def download(
        self,
        url: str,
        file_location: str,
        headers: Optional[Dict[str, str]] = None,
        enable_progress_bar: bool = False,
        indent_level: int = 0,
        artwork_settings: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> bool:
        """Download file from url to file_location."""
        pass


class NativeBackend(DownloadBackend):
    """Requests-based download backend with resume and progress reporting."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        continue_download: bool = True,
        settings: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        self.session = session or backend_session
        if settings is not None:
            resolved = resolve_downloader_settings(settings)
            self.continue_download = resolved['continue']
        else:
            self.continue_download = continue_download

    def download(
        self,
        url: str,
        file_location: str,
        headers: Optional[Dict[str, str]] = None,
        enable_progress_bar: bool = False,
        indent_level: int = 0,
        artwork_settings: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> bool:
        dir_path = os.path.dirname(file_location)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        req_headers = dict(headers) if headers else {}
        mode = 'wb'
        initial = 0
        existing_size = 0
        continue_download = kwargs.get('continue_download', self.continue_download)

        if continue_download and os.path.isfile(file_location):
            existing_size = os.path.getsize(file_location)
            if existing_size > 0:
                req_headers['Range'] = f'bytes={existing_size}-'

        try:
            r = self.session.get(url, stream=True, headers=req_headers, verify=False)
            if continue_download and existing_size > 0:
                if r.status_code == 416:
                    # Range not satisfiable -> file already fully downloaded
                    if artwork_settings and artwork_settings.get('should_resize', False):
                        resize_artwork(file_location, artwork_settings)
                    return True
                elif r.status_code == 206:
                    mode = 'ab'
                    initial = existing_size
                elif r.status_code == 200:
                    mode = 'wb'
                    initial = 0
                else:
                    r.raise_for_status()
            else:
                r.raise_for_status()

            total = None
            if 'content-length' in r.headers:
                content_len = int(r.headers['content-length'])
                total = initial + content_len

            with open(file_location, mode) as f:
                if enable_progress_bar and total:
                    bar = create_progress_bar(total=total, indent_level=indent_level, initial=initial)
                    try:
                        for chunk in r.iter_content(chunk_size=1024):
                            if chunk:
                                f.write(chunk)
                                if bar is not None:
                                    bar.update(len(chunk))
                    finally:
                        if bar is not None:
                            bar.close()
                else:
                    for chunk in r.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)

            if artwork_settings and artwork_settings.get('should_resize', False):
                resize_artwork(file_location, artwork_settings)
            return True

        except KeyboardInterrupt:
            if not continue_download and os.path.isfile(file_location):
                print(f'\tDeleting partially downloaded file "{str(file_location)}"')
                silentremove(file_location)
            raise KeyboardInterrupt


class Aria2cBackend(DownloadBackend):
    """Subprocess-based aria2c backend with argument building, progress parsing, and fallback."""

    def __init__(
        self,
        binary_path: Optional[str] = None,
        max_connections: Optional[int] = None,
        split: Optional[int] = None,
        min_split_size: Optional[str] = None,
        continue_download: Optional[bool] = None,
        extra_args: Optional[List[str]] = None,
        fallback_backend: Optional[DownloadBackend] = None,
        settings: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        resolved = resolve_downloader_settings(settings) if settings else {}
        self.binary_path = str(
            binary_path
            if binary_path is not None
            else resolved.get('binary_path', 'aria2c')
        )
        self.max_connections = int(
            max_connections
            if max_connections is not None
            else resolved.get('max_connections', 4)
        )
        self.split = int(
            split
            if split is not None
            else resolved.get('split', 4)
        )
        self.min_split_size = str(
            min_split_size
            if min_split_size is not None
            else resolved.get('min_split_size', '1M')
        )
        self.continue_download = bool(
            continue_download
            if continue_download is not None
            else resolved.get('continue', True)
        )
        if extra_args is not None:
            self.extra_args = list(extra_args)
        else:
            self.extra_args = list(resolved.get('extra_args', []))

        if fallback_backend is not None:
            self.fallback_backend = fallback_backend
        else:
            self.fallback_backend = NativeBackend(continue_download=self.continue_download)

    @classmethod
    def from_settings(cls, settings: Dict[str, Any], fallback_backend: Optional[DownloadBackend] = None) -> 'Aria2cBackend':
        return cls(settings=settings, fallback_backend=fallback_backend)

    def build_args(
        self,
        url: str,
        file_location: str,
        headers: Optional[Dict[str, str]] = None,
        enable_progress_bar: bool = False
    ) -> List[str]:
        return build_aria2c_args(
            binary_path=self.binary_path,
            url=url,
            file_location=file_location,
            max_connections=self.max_connections,
            split=self.split,
            min_split_size=self.min_split_size,
            continue_download=self.continue_download,
            extra_args=self.extra_args,
            headers=headers,
            enable_progress_bar=enable_progress_bar
        )

    def _fallback(self, reason: str, url: str, file_location: str, **kwargs) -> bool:
        msg = f'aria2c failure ({reason}), falling back to native download backend'
        warnings.warn(msg, UserWarning)
        print(f'Warning: {msg}')

        # BLOCKING FIX: aria2c with --split > 1 writes out-of-order chunks.
        # Handing this file to NativeBackend produces silent corruption when it
        # appends a sequential HTTP stream onto non-contiguous bytes. We MUST
        # delete both the partial output AND the .aria2 control file so
        # NativeBackend starts from byte 0 with no stale resume state.
        aria2_file = file_location + '.aria2'
        silentremove(aria2_file)      # control file FIRST
        silentremove(file_location)   # partial output SECOND

        # Defense in depth: force a fresh download even if continue_download=True
        # on this instance, so NativeBackend cannot attempt a Range resume against
        # a file that is now gone.
        fallback_kwargs = dict(kwargs)
        fallback_kwargs['continue_download'] = False

        return self.fallback_backend.download(url, file_location, **fallback_kwargs)

    def download(
        self,
        url: str,
        file_location: str,
        headers: Optional[Dict[str, str]] = None,
        enable_progress_bar: bool = False,
        indent_level: int = 0,
        artwork_settings: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> bool:
        binary_exists = shutil.which(self.binary_path) is not None or (
            os.path.isfile(self.binary_path) and os.access(self.binary_path, os.X_OK)
        )
        if not binary_exists:
            return self._fallback(
                f'aria2c binary not found at "{self.binary_path}"',
                url, file_location,
                headers=headers,
                enable_progress_bar=enable_progress_bar,
                indent_level=indent_level,
                artwork_settings=artwork_settings,
                **kwargs
            )

        dir_path = os.path.dirname(file_location)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        args = self.build_args(
            url=url,
            file_location=file_location,
            headers=headers,
            enable_progress_bar=enable_progress_bar
        )

        progress_pattern = ARIA2C_PROGRESS_RE
        bar = None
        process = None

        try:
            if enable_progress_bar:
                process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                try:
                    for line in process.stdout:
                        m = progress_pattern.search(line)
                        if m:
                            cur = parse_bytes(m.group(1))
                            total = parse_bytes(m.group(2))
                            if bar is None and total:
                                bar = create_progress_bar(total=total, indent_level=indent_level, initial=cur or 0)
                            elif bar is not None and cur is not None:
                                diff = cur - bar.n
                                if diff > 0:
                                    bar.update(diff)
                finally:
                    if process.stdout:
                        process.stdout.close()
                process.wait()
                if bar is not None:
                    if bar.total and bar.n < bar.total:
                        bar.update(bar.total - bar.n)
                    bar.close()
                    bar = None
            else:
                process = subprocess.run(
                    args,
                    capture_output=True,
                    text=True
                )

        except (FileNotFoundError, PermissionError) as e:
            if bar is not None:
                bar.close()
            return self._fallback(
                f'could not execute aria2c: {e}',
                url, file_location,
                headers=headers,
                enable_progress_bar=enable_progress_bar,
                indent_level=indent_level,
                artwork_settings=artwork_settings,
                **kwargs
            )
        except KeyboardInterrupt:
            if process is not None:
                if hasattr(process, 'poll') and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
            if bar is not None:
                bar.close()
            if not self.continue_download and os.path.isfile(file_location):
                print(f'\tDeleting partially downloaded file "{str(file_location)}"')
                silentremove(file_location)
                if os.path.isfile(file_location + '.aria2'):
                    silentremove(file_location + '.aria2')
            raise KeyboardInterrupt

        returncode = process.returncode
        if returncode != 0:
            return self._fallback(
                f'aria2c exited with return code {returncode}',
                url, file_location,
                headers=headers,
                enable_progress_bar=enable_progress_bar,
                indent_level=indent_level,
                artwork_settings=artwork_settings,
                **kwargs
            )

        if artwork_settings and artwork_settings.get('should_resize', False):
            resize_artwork(file_location, artwork_settings)
        return True


def get_backend(backend_type: Optional[str] = None, settings: Optional[Dict[str, Any]] = None) -> DownloadBackend:
    resolved = resolve_downloader_settings(settings)
    if backend_type is None:
        backend_type = resolved.get('backend', 'native')
    backend_type = str(backend_type or 'native').lower().strip()

    if backend_type == 'aria2c':
        return Aria2cBackend(settings=resolved)
    return NativeBackend(settings=resolved)
