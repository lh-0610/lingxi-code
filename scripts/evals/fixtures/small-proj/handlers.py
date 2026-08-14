from utils import normalize_path


def load(p):
    return open(normalize_path(p)).read()


def save(p, data):
    with open(normalize_path(p), "w") as f:
        f.write(data)
