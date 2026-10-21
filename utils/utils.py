import pickle, requests, errno, hashlib, math, os, re, operator
from tqdm import tqdm
from PIL import Image, ImageChops
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import reduce
from orpheus.download_backend import (
    DownloadBackend,
    NativeBackend,
    Aria2cBackend,
    get_backend,
    build_aria2c_args,
    resolve_downloader_settings,
    get_global_downloader_settings,
    set_global_downloader_settings,
    create_progress_bar,
    parse_bytes,
    resize_artwork
)


def hash_string(input_str: str, hash_type: str = 'MD5'):
    if hash_type == 'MD5':
        return hashlib.md5(input_str.encode("utf-8")).hexdigest()
    else:
        raise Exception('Invalid hash type selected')

def create_requests_session():
    session_ = requests.Session()
    retries = Retry(total=10, backoff_factor=0.4, status_forcelist=[429, 500, 502, 503, 504])
    session_.mount('http://', HTTPAdapter(max_retries=retries))
    session_.mount('https://', HTTPAdapter(max_retries=retries))
    return session_

sanitise_name = lambda name : re.sub(r'[:]', ' - ', re.sub(r'[\\/*?"<>|$]', '', re.sub(r'[ \t]+$', '', str(name).rstrip()))) if name else ''


def fix_byte_limit(path: str, byte_limit=250):
    # only needs the relative path, the abspath uses already existing folders
    rel_path = os.path.relpath(path).replace('\\', '/')

    # split path into directory and filename
    directory, filename = os.path.split(rel_path)

    # truncate filename if its byte size exceeds the byte_limit
    filename_bytes = filename.encode('utf-8')
    fixed_bytes = filename_bytes[:byte_limit]
    fixed_filename = fixed_bytes.decode('utf-8', 'ignore')

    # join the directory and truncated filename together
    return directory + '/' + fixed_filename


r_session = create_requests_session()

def download_file(url, file_location, headers={}, enable_progress_bar=False, indent_level=0, artwork_settings=None, downloader_settings=None):
    if downloader_settings is None:
        downloader_settings = get_global_downloader_settings()

    resolved = resolve_downloader_settings(downloader_settings)
    continue_download = resolved.get('continue', True)

    has_aria2_partial = os.path.isfile(file_location + '.aria2')
    if os.path.isfile(file_location) and not has_aria2_partial and not continue_download:
        return None

    backend = get_backend(resolved.get('backend', 'native'), settings=resolved)
    return backend.download(
        url=url,
        file_location=file_location,
        headers=headers,
        enable_progress_bar=enable_progress_bar,
        indent_level=indent_level,
        artwork_settings=artwork_settings
    )

# root mean square code by Charlie Clark: https://code.activestate.com/recipes/577630-comparing-two-images/
def compare_images(image_1, image_2):
    with Image.open(image_1) as im1, Image.open(image_2) as im2:
        h = ImageChops.difference(im1, im2).convert('L').histogram()
        return math.sqrt(reduce(operator.add, map(lambda h, i: h*(i**2), h, range(256))) / (float(im1.size[0]) * im1.size[1]))

# TODO: check if not closing the files causes issues, and see if there's a way to use the context manager with lambda expressions
get_image_resolution = lambda image_location : Image.open(image_location).size[0]

def silentremove(filename):
    try:
        os.remove(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

def read_temporary_setting(settings_location, module, root_setting=None, setting=None, global_mode=False):
    temporary_settings = pickle.load(open(settings_location, 'rb'))
    module_settings = temporary_settings['modules'][module] if module in temporary_settings['modules'] else None
    
    if module_settings:
        if global_mode:
            session = module_settings
        else:
            session = module_settings['sessions'][module_settings['selected']]
    else:
        session = None

    if session and root_setting:
        if setting:
            return session[root_setting][setting] if root_setting in session and setting in session[root_setting] else None
        else:
            return session[root_setting] if root_setting in session else None
    elif root_setting and not session:
        raise Exception('Module does not use temporary settings') 
    else:
        return session

def set_temporary_setting(settings_location, module, root_setting, setting=None, value=None, global_mode=False):
    temporary_settings = pickle.load(open(settings_location, 'rb'))
    module_settings = temporary_settings['modules'][module] if module in temporary_settings['modules'] else None

    if module_settings:
        if global_mode:
            session = module_settings
        else:
            session = module_settings['sessions'][module_settings['selected']]
    else:
        session = None

    if not session:
        raise Exception('Module does not use temporary settings')
    if setting:
        session[root_setting][setting] = value
    else:
        session[root_setting] = value
    pickle.dump(temporary_settings, open(settings_location, 'wb'))

create_temp_filename = lambda : f'temp/{os.urandom(16).hex()}'

def save_to_temp(input: bytes):
    location = create_temp_filename()
    open(location, 'wb').write(input)
    return location

def download_to_temp(url, headers={}, extension='', enable_progress_bar=False, indent_level=0, downloader_settings=None):
    location = create_temp_filename() + (('.' + extension) if extension else '')
    download_file(url, location, headers=headers, enable_progress_bar=enable_progress_bar, indent_level=indent_level, downloader_settings=downloader_settings)
    return location
