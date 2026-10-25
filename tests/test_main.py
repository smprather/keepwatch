import subprocess
import sys

from watchglass import __version__


def test_python_dash_m_watchglass():
    result = subprocess.run([sys.executable, "-m", "watchglass", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert __version__ in result.stdout
