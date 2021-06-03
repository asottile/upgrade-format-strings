import pytest

from pyupgrade._data import Settings
from pyupgrade._main import _fix_plugins


@pytest.mark.parametrize(
    's',
    (
        # we timidly skip `if` without `else` as it could cause a SyntaxError
        'if six.PY2:\n'
        '    pass',
        # here's the case where it causes a SyntaxError
        'if True:\n'
        '    if six.PY2:\n'
        '        pass\n',
        # for now we don't attempt to rewrite `elif`
        'if six.PY2:\n'
        '    pass\n'
        'elif False:\n'
        '    pass\n',
        'if six.PY3:\n'
        '    pass\n'
        'elif False:\n'
        '    pass\n',
        # don't rewrite version compares with not 3.0 compares
        'if sys.version_info >= (3, 6):\n'
        '    3.6\n'
        'else:\n'
        '    3.5\n',
        # don't try and think about `sys.version`
        'from sys import version\n'
        'if sys.version[0] > "2":\n'
        '    3\n'
        'else:\n'
        '    2\n',
        pytest.param(
            'from .sys import version_info\n'
            'if version_info < (3,):\n'
            '    print("2")\n'
            'else:\n'
            '    print("3")\n',
            id='relative imports',
        ),
    ),
)
def test_fix_py2_block_noop(s):
    assert _fix_plugins(s, settings=Settings(min_version=(3,))) == s


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        pytest.param(
            'if six.PY2:\n'
            '    print("py2")\n'
            'else:\n'
            '    print("py3")\n',

            'print("py3")\n',

            id='six.PY2',
        ),
        pytest.param(
            'if six.PY2:\n'
            '    if True:\n'
            '        print("py2!")\n'
            '    else:\n'
            '        print("???")\n'
            'else:\n'
            '    print("py3")\n',

            'print("py3")\n',

            id='six.PY2, nested ifs',
        ),
        pytest.param(
            'if six.PY2: print("PY2!")\n'
            'else:       print("PY3!")\n',

            'print("PY3!")\n',

            id='six.PY2, oneline',
        ),
        pytest.param(
            'if six.PY2: print("PY2!")\n'
            'else:       print("PY3!")',

            'print("PY3!")',

            id='six.PY2, oneline, no newline at end of file',
        ),
        pytest.param(
            'if True:\n'
            '    if six.PY2:\n'
            '        print("PY2")\n'
            '    else:\n'
            '        print("PY3")\n',

            'if True:\n'
            '    print("PY3")\n',

            id='six.PY2, indented',
        ),
        pytest.param(
            'if six.PY2: print(1 if True else 3)\n'
            'else:\n'
            '   print("py3")\n',

            'print("py3")\n',

            id='six.PY2, `else` token inside oneline if\n',
        ),
        pytest.param(
            'if six.PY2:\n'
            '    def f():\n'
            '        print("py2")\n'
            'else:\n'
            '    def f():\n'
            '        print("py3")\n',

            'def f():\n'
            '    print("py3")\n',

            id='six.PY2, multiple indents in block',
        ),
        pytest.param(
            'if not six.PY2:\n'
            '    print("py3")\n'
            'else:\n'
            '    print("py2")\n'
            '\n'
            '\n'
            'x = 1\n',

            'print("py3")\n'
            '\n'
            '\n'
            'x = 1\n',

            id='not six.PY2, remove second block',
        ),
        pytest.param(
            'if not six.PY2:\n'
            '    print("py3")\n'
            'else:\n'
            '    print("py2")',

            'print("py3")\n',

            id='not six.PY2, no end of line',
        ),
        pytest.param(
            'if not six.PY2:\n'
            '    print("py3")\n'
            'else:\n'
            '    print("py2")\n'
            '    # ohai\n'
            '\n'
            'x = 1\n',

            'print("py3")\n'
            '\n'
            'x = 1\n',

            id='not six.PY2: else block ends in comment',
        ),
        pytest.param(
            'if not six.PY2: print("py3")\n'
            'else: print("py2")\n',

            'print("py3")\n',

            id='not six.PY2, else is single line',
        ),
        pytest.param(
            'if six.PY3:\n'
            '    print("py3")\n'
            'else:\n'
            '    print("py2")\n',

            'print("py3")\n',

            id='six.PY3',
        ),
        pytest.param(
            'if True:\n'
            '    if six.PY3:\n'
            '        print("py3")\n'
            '    else:\n'
            '        print("py2")\n',

            'if True:\n'
            '    print("py3")\n',

            id='indented six.PY3',
        ),
        pytest.param(
            'from six import PY3\n'
            'if not PY3:\n'
            '    print("py2")\n'
            'else:\n'
            '    print("py3")\n',

            'from six import PY3\n'
            'print("py3")\n',

            id='not PY3',
        ),
        pytest.param(
            'def f():\n'
            '    if six.PY2:\n'
            '        try:\n'
            '            yield\n'
            '        finally:\n'
            '            pass\n'
            '    else:\n'
            '        yield\n',

            'def f():\n'
            '    yield\n',

            id='six.PY2, finally',
        ),
        pytest.param(
            'class C:\n'
            '    def g():\n'
            '        pass\n'
            '\n'
            '    if six.PY2:\n'
            '        def f(py2):\n'
            '            pass\n'
            '    else:\n'
            '        def f(py3):\n'
            '            pass\n'
            '\n'
            '    def h():\n'
            '        pass\n',

            'class C:\n'
            '    def g():\n'
            '        pass\n'
            '\n'
            '    def f(py3):\n'
            '        pass\n'
            '\n'
            '    def h():\n'
            '        pass\n',

            id='six.PY2 in class\n',
        ),
        pytest.param(
            'if True:\n'
            '    if six.PY2:\n'
            '        2\n'
            '    else:\n'
            '        3\n'
            '\n'
            '    # comment\n',

            'if True:\n'
            '    3\n'
            '\n'
            '    # comment\n',

            id='six.PY2, comment after',
        ),
        pytest.param(
            'if six.PY2:\n'
            '    def f():\n'
            '        print("py2")\n'
            '    def g():\n'
            '        print("py2")\n'
            'else:\n'
            '    def f():\n'
            '        print("py3")\n'
            '    def g():\n'
            '        print("py3")\n',

            'def f():\n'
            '    print("py3")\n'
            'def g():\n'
            '    print("py3")\n',

            id='six.PY2 multiple functions',
        ),
        pytest.param(
            'if True:\n'
            '    if six.PY3:\n'
            '        3\n'
            '    else:\n'
            '        2\n'
            '\n'
            '    # comment\n',

            'if True:\n'
            '    3\n'
            '\n'
            '    # comment\n',

            id='six.PY3, comment after',
        ),
        pytest.param(
            'if sys.version_info == 2:\n'
            '    2\n'
            'else:\n'
            '    3\n',

            '3\n',

            id='sys.version_info == 2',
        ),
        pytest.param(
            'if sys.version_info < (3,):\n'
            '    2\n'
            'else:\n'
            '    3\n',

            '3\n',

            id='sys.version_info < (3,)',
        ),
        pytest.param(
            'if sys.version_info < (3, 0):\n'
            '    2\n'
            'else:\n'
            '    3\n',

            '3\n',

            id='sys.version_info < (3, 0)',
        ),
        pytest.param(
            'if sys.version_info == 3:\n'
            '    3\n'
            'else:\n'
            '    2\n',

            '3\n',

            id='sys.version_info == 3',
        ),
        pytest.param(
            'if sys.version_info > (3,):\n'
            '    3\n'
            'else:\n'
            '    2\n',

            '3\n',

            id='sys.version_info > (3,)',
        ),
        pytest.param(
            'if sys.version_info >= (3,):\n'
            '    3\n'
            'else:\n'
            '    2\n',

            '3\n',

            id='sys.version_info >= (3,)',
        ),
        pytest.param(
            'from sys import version_info\n'
            'if version_info > (3,):\n'
            '    3\n'
            'else:\n'
            '    2\n',

            'from sys import version_info\n'
            '3\n',

            id='from sys import version_info, > (3,)',
        ),
        pytest.param(
            'if True:\n'
            '    print(1)\n'
            'elif six.PY2:\n'
            '    print(2)\n'
            'else:\n'
            '    print(3)\n',

            'if True:\n'
            '    print(1)\n'
            'else:\n'
            '    print(3)\n',

            id='elif six.PY2 else',
        ),
        pytest.param(
            'if True:\n'
            '    print(1)\n'
            'elif six.PY3:\n'
            '    print(3)\n'
            'else:\n'
            '    print(2)\n',

            'if True:\n'
            '    print(1)\n'
            'else:\n'
            '    print(3)\n',

            id='elif six.PY3 else',
        ),
        pytest.param(
            'if True:\n'
            '    print(1)\n'
            'elif six.PY3:\n'
            '    print(3)\n',

            'if True:\n'
            '    print(1)\n'
            'else:\n'
            '    print(3)\n',

            id='elif six.PY3 no else',
        ),
        pytest.param(
            'if True:\n'
            '    if sys.version_info > (3,):\n'
            '        print(3)\n'
            '    # comment\n'
            '    print(2+3)\n',

            'if True:\n'
            '    print(3)\n'
            '    # comment\n'
            '    print(2+3)\n',

            id='comment after dedented block',
        ),
    ),
)
def test_fix_py2_blocks(s, expected):
    ret = _fix_plugins(s, settings=Settings(min_version=(3,)))
    assert ret == expected


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        ('if six.PY3: print(3)\n', 'print(3)\n'),
        (
            'if six.PY3:\n'
            '    print(3)\n',

            'print(3)\n',
        ),
    ),
)
def test_fix_py3_only_code(s, expected):
    ret = _fix_plugins(s, settings=Settings(min_version=(3,)))
    assert ret == expected
