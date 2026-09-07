import os


def _env(name: str, default: str) -> str:
    """Like os.environ.get, but treats an empty string as 'not set' too
    (env_file entries like FOO= otherwise override real defaults with '')."""
    value = os.environ.get(name)
    return value if value else default
