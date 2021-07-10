import pytest

from pyupgrade._data import Settings
from pyupgrade._main import _fix_plugins


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        (
            'def f():\n'
            '    for x in y:\n'
            '        yield x',
            'def f():\n'
            '    yield from y\n',
        ),
        (
            'def f():\n'
            '    for x in [1, 2, 3]:\n'
            '        yield x',
            'def f():\n'
            '    yield from [1, 2, 3]\n',
        ),
        (
            'def f():\n'
            '    for x in {x for x in y}:\n'
            '        yield x',
            'def f():\n'
            '    yield from {x for x in y}\n',
        ),
        (
            'def f():\n'
            '    for x in (1, 2, 3):\n'
            '        yield x',
            'def f():\n'
            '    yield from (1, 2, 3)\n',
        ),
        (
            'def f():\n'
            '    for x, y in {3: "x", 6: "y"}:\n'
            '        yield x, y',
            'def f():\n'
            '    yield from {3: "x", 6: "y"}\n',
        ),
        (
            'def f():  # Comment one\n'
            '    # Comment two\n'
            '    for x, y in {  # Comment three\n'
            '       3: "x",  # Comment four\n'
            '       # Comment five\n'
            '       6: "y"  # Comment six\n'
            '    }:  # Comment seven\n'
            '       # Comment eight\n'
            '       yield x, y  # Comment nine\n'
            '       # Comment ten',
            'def f():  # Comment one\n'
            '    # Comment two\n'
            '    yield from {  # Comment three\n'
            '       3: "x",  # Comment four\n'
            '       # Comment five\n'
            '       6: "y"  # Comment six\n'
            '    }\n',
        ),
        (
            'def f():\n'
            '    for x, y in [{3: (3, [44, "long ss"]), 6: "y"}]:\n'
            '        yield x, y',
            'def f():\n'
            '    yield from [{3: (3, [44, "long ss"]), 6: "y"}]\n',
        ),
        (
            'def f():\n'
            '    for x, y in z():\n'
            '        yield x, y',
            'def f():\n'
            '    yield from z()\n',
        ),
        (
            'def f():\n'
            '    def func():\n'
            '        # This comment is preserved\n'
            '\n'
            '        for x, y in z():  # Comment one\n'
            '\n'
            '            # Commment two\n'
            '            yield x, y  # Comment three\n'
            '            # Comment four\n'
            '\n\n'
            '# Comment\n'
            'def g():\n'
            '    print(3)',
            'def f():\n'
            '    def func():\n'
            '        # This comment is preserved\n'
            '\n'
            '        yield from z()\n'
            '\n\n'
            '# Comment\n'
            'def g():\n'
            '    print(3)',
        ),
        (
            'async def f():\n'
            '    for x in [1, 2]:\n'
            '        yield x\n'
            '\n'
            '    def g():\n'
            '        for x in [1, 2, 3]:\n'
            '            yield x\n'
            '\n'
            '    for x in [1, 2]:\n'
            '        yield x\n'
            '\n'
            '    return g',
            'async def f():\n'
            '    for x in [1, 2]:\n'
            '        yield x\n'
            '\n'
            '    def g():\n'
            '        yield from [1, 2, 3]\n'
            '\n'
            '    for x in [1, 2]:\n'
            '        yield x\n'
            '\n'
            '    return g',
        ),
        pytest.param(
            'def f():\n'
            '    for x in y:\n'
            '        yield x\n'
            '    for z in x:\n'
            '        yield z\n',
            'def f():\n'
            '    for x in y:\n'
            '        yield x\n'
            '    yield from x\n',
            id='leave one loop alone (referenced after assignment)',
        ),
    ),
)
def test_fix_yield_from(s, expected):
    assert _fix_plugins(s, settings=Settings(min_version=(3,))) == expected


@pytest.mark.parametrize(
    's',
    (
        'def f():\n'
        '    for x in z:\n'
        '        yield',
        'def f():\n'
        '    for x in z:\n'
        '        yield y',
        'def f():\n'
        '    for x, y in z:\n'
        '        yield x',
        'def f():\n'
        '    for x, y in z:\n'
        '        yield y',
        'def f():\n'
        '    for a, b in z:\n'
        '        yield x, y',
        'def f():\n'
        '    for x, y in z:\n'
        '        yield y, x',
        'def f():\n'
        '    for x, y, c in z:\n'
        '        yield x, y',
        'def f():\n'
        '    for x in z:\n'
        '        x = 22\n'
        '        yield x',
        'def f():\n'
        '    for x in z:\n'
        '        yield x\n'
        '    else:\n'
        '        print("boom!")\n',
        pytest.param(
            'def f():\n'
            '    for x in range(5):\n'
            '        yield x\n'
            '    print(x)\n',
            id='variable referenced after loop',
        ),
        pytest.param(
            'def f():\n'
            '    def g():\n'
            '        print(x)\n'
            '    for x in range(5):\n'
            '        yield x\n'
            '    g()\n',
            id='variable referenced after loop, but via function',
        ),
        pytest.param(
            'def f():\n'
            '    def g():\n'
            '        def h():\n'
            '           print(x)\n'
            '        return h\n'
            '    for x in range(5):\n'
            '        yield x\n'
            '    g()()\n',
            id='variable referenced after loop, but via nested function',
        ),
        pytest.param(
            'def f(x):\n'
            '    del x\n',
            id='regression with del ctx (#306)',
        ),
    ),
)
def test_fix_yield_from_noop(s):
    assert _fix_plugins(s, settings=Settings(min_version=(3,))) == s
