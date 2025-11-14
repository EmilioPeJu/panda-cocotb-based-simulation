import os

from pathlib import Path


def get_panda_path():
    return Path(os.getenv('panda_src_dir'))


def get_config_path():
    return Path(os.getenv('panda_config_dir'))


def get_extra_path():
    top = Path(__file__).parent.parent.resolve()
    return top / 'hdl'
