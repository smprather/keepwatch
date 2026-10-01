import subprocess
import sys

from keepwatch import __version__


def test_python_dash_m_keepwatch():
    result = subprocess.run([sys.executable, "-m", "keepwatch", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert __version__ in result.stdout
