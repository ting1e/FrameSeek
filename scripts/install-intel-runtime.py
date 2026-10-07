"""Install a pinned Intel OpenCL runtime from Intel's official release packages."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

# https://github.com/intel/compute-runtime/releases/tag/24.35.30872.22
PACKAGES = [
    ('intel/intel-graphics-compiler', 'igc-1.0.17537.20', 'intel-igc-core_1.0.17537.20_amd64.deb',
     '7f2af5b0e567a43625a748effb744d0b3c96acf805467d099e46eee617e11b2a'),
    ('intel/intel-graphics-compiler', 'igc-1.0.17537.20', 'intel-igc-opencl_1.0.17537.20_amd64.deb',
     'ac2088331d55c7de15bd57373f73630e95b40e1275934bcfd96bf0c3e03769a7'),
    ('intel/compute-runtime', '24.35.30872.22', 'intel-opencl-icd_24.35.30872.22_amd64.deb',
     '92985888765be55e8ee54827c3e04ae8df93e7410733073ef7b4e42c3d40e24d'),
    ('intel/compute-runtime', '24.35.30872.22', 'libigdgmm12_22.5.0_amd64.deb',
     'cc29d14df83cff1b3c6a66baa39257f0211b168ab43a99c2dc62a3734431bc23'),
]

with tempfile.TemporaryDirectory(prefix='intel-runtime-') as temporary:
    packages = []
    for repository, version, name, checksum in PACKAGES:
        destination = Path(temporary) / name
        url = f'https://github.com/{repository}/releases/download/{version}/{name}'
        print('Downloading Intel runtime package:', name, flush=True)
        with urllib.request.urlopen(url, timeout=180) as source, destination.open('wb') as output:
            shutil.copyfileobj(source, output)
        with destination.open('rb') as source:
            actual = hashlib.file_digest(source, 'sha256').hexdigest()
        if actual != checksum:
            raise RuntimeError('Intel package checksum mismatch: ' + name)
        packages.append(str(destination))
    subprocess.run(['apt-get', 'update'], check=True)
    subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends',
                    'ocl-icd-libopencl1', *packages], check=True)
