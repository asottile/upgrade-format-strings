import sys

import pytest

from pyupgrade._data import Settings
from pyupgrade._main import _fix_plugins


@pytest.mark.parametrize(
    's',
    (
        # syntax error
        'x = (',
        # unrelated
        'from os import path',
        'from six import moves',
        'a[0]()',
        # unrelated decorator
        '@mydec\n'
        'class C: pass',
        # don't rewrite things that would become `raise` in non-statements
        'print(six.raise_from(exc, exc_from))',
        # intentionally not handling this case due to it being a bug (?)
        'class C(six.with_metaclass(Meta, B), D): pass',
        # cannot determine args to rewrite them
        'six.reraise(*err)', 'six.u(*a)',
        'class C(six.with_metaclass(*a)): pass',
        '@six.add_metaclass(*a)\n'
        'class C: pass\n',
        # next is shadowed
        'next()',
        ('traceback.format_exc(*sys.exc_info())'),
        pytest.param('six.iteritems()', id='wrong argument count'),
        pytest.param(
            'from six import iteritems as items\n'
            'items(foo)\n',
            id='ignore as renaming',
        ),
    ),
)
def test_fix_six_noop(s):
    assert _fix_plugins(s, settings=Settings(min_version=(3,))) == s


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        (
            'six.byte2int(b"f")',
            'b"f"[0]',
        ),
        (
            'six.get_unbound_function(meth)\n',
            'meth\n',
        ),
        (
            'from six import get_unbound_function\n'
            'get_unbound_function(meth)\n',

            'from six import get_unbound_function\n'
            'meth\n',
        ),
        (
            'six.indexbytes(bs, i)\n',
            'bs[i]\n',
        ),
        (
            'six.assertCountEqual(\n'
            '   self,\n'
            '   arg1,\n'
            '   arg2,\n'
            ')',

            'self.assertCountEqual(\n'
            '   arg1,\n'
            '   arg2,\n'
            ')',
        ),
        (
            'six.assertCountEqual(\n'
            '   self,\\\n'
            '   arg1,\n'
            '   arg2,\n'
            ')',

            'self.assertCountEqual(\\\n'
            '   arg1,\n'
            '   arg2,\n'
            ')',
        ),
        (
            'six.assertCountEqual(\n'
            '   self,  # hello\n'
            '   arg1,\n'
            '   arg2,\n'
            ')',

            'self.assertCountEqual(\n'
            '   arg1,\n'
            '   arg2,\n'
            ')',
        ),
        (
            'six.assertCountEqual(\n'
            '   self,\n'
            '   arg1,\n'
            '   (1, 2, 3),\n'
            ')',

            'self.assertCountEqual(\n'
            '   arg1,\n'
            '   (1, 2, 3),\n'
            ')',
        ),
        pytest.param(
            'six.u ("bar")',
            '"bar"',
            id='weird spacing six.u',
        ),
        pytest.param(
            'from six import u\nu ("bar")',
            'from six import u\n"bar"',
            id='weird spacing u',
        ),
        (
            'six.raise_from(exc, exc_from)\n',
            'raise exc from exc_from\n',
        ),
        (
            'six.reraise(tp, exc, tb)\n',
            'raise exc.with_traceback(tb)\n',
        ),
        (
            'six.reraise(tp, exc)\n',
            'raise exc.with_traceback(None)\n',
        ),
        (
            'six.reraise(*sys.exc_info())\n',
            'raise\n',
        ),
        (
            'from sys import exc_info\n'
            'six.reraise(*exc_info())\n',

            'from sys import exc_info\n'
            'raise\n',
        ),
        (
            'from six import raise_from\n'
            'raise_from(exc, exc_from)\n',

            'from six import raise_from\n'
            'raise exc from exc_from\n',
        ),
        (
            'six.reraise(\n'
            '   tp,\n'
            '   exc,\n'
            '   tb,\n'
            ')\n',
            'raise exc.with_traceback(tb)\n',
        ),
        pytest.param(
            'six.raise_from (exc, exc_from)',
            'raise exc from exc_from',
            id='weird spacing six.raise_from',
        ),
        pytest.param(
            'from six import raise_from\nraise_from (exc, exc_from)',
            'from six import raise_from\nraise exc from exc_from',
            id='weird spacing raise_from',
        ),
        (
            'class C(six.with_metaclass(M)): pass',

            'class C(metaclass=M): pass',
        ),
        (
            'class C(six.with_metaclass(M, B)): pass',

            'class C(B, metaclass=M): pass',
        ),
        (
            'class C(six.with_metaclass(M, B1, B2)): pass',

            'class C(B1, B2, metaclass=M): pass',
        ),
        (
            'from six import with_metaclass\n'
            'class C(with_metaclass(M, B)): pass\n',

            'from six import with_metaclass\n'
            'class C(B, metaclass=M): pass\n',
        ),
        pytest.param(
            'class C(six.with_metaclass (M, B)): pass',
            'class C(B, metaclass=M): pass',
            id='weird spacing six.with_metaclass',
        ),
        pytest.param(
            'from six import with_metaclass\n'
            'class C(with_metaclass (M, B)): pass',

            'from six import with_metaclass\n'
            'class C(B, metaclass=M): pass',

            id='weird spacing with_metaclass',
        ),
        pytest.param(
            'from six import with_metaclass\n'
            'class C(with_metaclass(M, object)): pass',

            'from six import with_metaclass\n'
            'class C(metaclass=M): pass',

            id='elide object base in with_metaclass',
        ),
        pytest.param(
            'class C(six.with_metaclass(M, B,)): pass',

            'class C(B, metaclass=M): pass',

            id='with_metaclass and trailing comma',
        ),
        pytest.param(
            '@six.add_metaclass(M)\n'
            'class C: pass\n',

            'class C(metaclass=M): pass\n',

            id='basic six.add_metaclass',
        ),
        pytest.param(
            'from six import add_metaclass\n'
            '@add_metaclass(M)\n'
            'class C: pass\n',

            'from six import add_metaclass\n'
            'class C(metaclass=M): pass\n',

            id='basic add_metaclass',
        ),
        pytest.param(
            '@six.add_metaclass(M)\n'
            'class C(): pass\n',

            'class C(metaclass=M): pass\n',

            id='basic six.add_metaclass, no bases but parens',
        ),
        pytest.param(
            '@six.add_metaclass(M)\n'
            'class C(A): pass\n',

            'class C(A, metaclass=M): pass\n',

            id='add_metaclass, one base',
        ),
        pytest.param(
            '@six.add_metaclass(M)\n'
            'class C(\n'
            '    A,\n'
            '): pass\n',

            'class C(\n'
            '    A, metaclass=M,\n'
            '): pass\n',

            id='add_metaclass, base with trailing comma',
        ),
        pytest.param(
            'x = (object,)\n'
            '@six.add_metaclass(M)\n'
            'class C(x[:][0]): pass\n',

            'x = (object,)\n'
            'class C(x[:][0], metaclass=M): pass\n',

            id='add_metaclass, weird base that contains a :',
        ),
        pytest.param(
            'if True:\n'
            '    @six.add_metaclass(M)\n'
            '    class C: pass\n',

            'if True:\n'
            '    class C(metaclass=M): pass\n',

            id='add_metaclass, indented',
        ),
        pytest.param(
            'print(six.itervalues({1:2}))\n',
            'print({1:2}.values())\n',
            id='six.itervalues',
        ),
        pytest.param(
            'print(next(six.itervalues({1:2})))\n',
            'print(next(iter({1:2}.values())))\n',
            id='six.itervalues inside next(...)',
        ),
        pytest.param(
            'for _ in six.itervalues({} or y): pass',
            'for _ in ({} or y).values(): pass',
            id='needs parenthesizing for BoolOp',
        ),
        pytest.param(
            'for _ in six.itervalues({} | y): pass',
            'for _ in ({} | y).values(): pass',
            id='needs parenthesizing for BinOp',
        ),
        pytest.param(
            'six.int2byte(x | y)',
            'bytes((x | y,))',
            id='no parenthesize for int2byte BinOP',
        ),
        pytest.param(
            'six.iteritems(+weird_dct)',
            '(+weird_dct).items()',
            id='needs parenthesizing for UnaryOp',
        ),
        pytest.param(
            'x = six.get_method_function(lambda: x)',
            'x = (lambda: x).__func__',
            id='needs parenthesizing for Lambda',
        ),
        pytest.param(
            'for _ in six.itervalues(x if 1 else y): pass',
            'for _ in (x if 1 else y).values(): pass',
            id='needs parenthesizing for IfExp',
        ),
        # this one is bogus / impossible, but parenthesize it anyway
        pytest.param(
            'six.itervalues(x for x in y)',
            '(x for x in y).values()',
            id='needs parentehsizing for GeneratorExp',
        ),
        pytest.param(
            'async def f():\n'
            '    return six.iteritems(await y)\n',
            'async def f():\n'
            '    return (await y).items()\n',
            id='needs parenthesizing for Await',
        ),
        # this one is bogus / impossible, but parenthesize it anyway
        pytest.param(
            'six.itervalues(x < y)',
            '(x < y).values()',
            id='needs parentehsizing for Compare',
        ),
        pytest.param(
            'x = six.itervalues(\n'
            '    # comment\n'
            '    x,\n'
            ')',
            'x = (\n'
            '    # comment\n'
            '    x,\n'
            ').values()',
            id='multiline first argument with comment',
        ),
    ),
)
def test_fix_six(s, expected):
    ret = _fix_plugins(s, settings=Settings(min_version=(3,)))
    assert ret == expected


@pytest.mark.xfail(sys.version_info < (3, 8), reason='walrus')
@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        pytest.param(
            'for _ in six.itervalues(x := y): pass',
            'for _ in (x := y).values(): pass',
            id='needs parenthesizing for NamedExpr',
        ),
    ),
)
def test_fix_six_py38_plus(s, expected):
    ret = _fix_plugins(s, settings=Settings(min_version=(3,)))
    assert ret == expected


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        (
            'import six\n\nclass C(six.Iterator): pass',
            'import six\n\nclass C: pass',
        ),
        (
            'from six import Iterator\n'
            '\n'
            'class C(Iterator): pass',
            'from six import Iterator\n'
            '\n'
            'class C: pass',
        ),
        (
            'import six\n'
            '\n'
            'class C(\n'
            '    six.Iterator,\n'
            '): pass',
            'import six\n'
            '\n'
            'class C: pass',
        ),
        (
            'class C(object, six.Iterator): pass',
            'class C: pass',
        ),
        (
            'class C(six.Iterator, metaclass=ABCMeta): pass',
            'class C(metaclass=ABCMeta): pass',
        ),
        (
            'class C(six.Iterator, object, metaclass=ABCMeta): pass',
            'class C(metaclass=ABCMeta): pass',
        ),
        (
            'from six import Iterator\n'
            '\n'
            'class C(Iterator, metaclass=ABCMeta): pass',
            'from six import Iterator\n'
            '\n'
            'class C(metaclass=ABCMeta): pass',
        ),
    ),
)
def test_fix_base_classes(s, expected):
    ret = _fix_plugins(s, settings=Settings(min_version=(3,)))
    assert ret == expected
