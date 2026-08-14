
import sys

sys.platform = 'linux'
import pytest

pytest.main(['--ignore-glob=tests/test_sandbox*.py', '-q'])

