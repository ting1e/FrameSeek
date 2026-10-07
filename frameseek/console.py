import sys


def configure_console():
    # Windows legacy consoles and redirected logs must support NAS Unicode paths.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='backslashreplace')
