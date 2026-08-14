import os


def normalize_path(p):
    return os.path.abspath(os.path.expanduser(p))
