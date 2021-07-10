import pytest

from pyupgrade._main import _fix_plugins
from pyupgrade._main import Settings


@pytest.mark.parametrize(
    's',
    (
        # syntax error
        'x(',

        'class C(Base):\n'
        '    def f(self):\n'
        '        super().f()\n',

        # super class doesn't match class name
        'class C(Base):\n'
        '    def f(self):\n'
        '        super(Base, self).f()\n',

        # super outside of a class (technically legal!)
        'def f(self):\n'
        '    super(C, self).f()\n',

        # super used in a comprehension
        'class C(Base):\n'
        '    def f(self):\n'
        '        return [super(C, self).f() for _ in ()]\n',
        'class C(Base):\n'
        '    def f(self):\n'
        '        return {super(C, self).f() for _ in ()}\n',
        'class C(Base):\n'
        '    def f(self):\n'
        '        return (super(C, self).f() for _ in ())\n',
        'class C(Base):\n'
        '    def f(self):\n'
        '        return {True: super(C, self).f() for _ in ()}\n',
        # nested comprehension
        'class C(Base):\n'
        '    def f(self):\n'
        '        return [\n'
        '            (\n'
        '                [_ for _ in ()],\n'
        '                super(C, self).f(),\n'
        '            )\n'
        '            for _ in ()'
        '        ]\n',
        # super in a closure
        'class C(Base):\n'
        '    def f(self):\n'
        '        def g():\n'
        '            super(C, self).f()\n'
        '        g()\n',
        'class C(Base):\n'
        '    def f(self):\n'
        '        g = lambda: super(C, self).f()\n'
        '        g()\n',
    ),
)
def test_fix_super_noop(s):
    assert _fix_plugins(s, settings=Settings(min_version=(3,))) == s


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        (
            'class C(Base):\n'
            '    def f(self):\n'
            '        super(C, self).f()\n',
            'class C(Base):\n'
            '    def f(self):\n'
            '        super().f()\n',
        ),
        (
            'class C(Base):\n'
            '    def f(self):\n'
            '        super (C, self).f()\n',
            'class C(Base):\n'
            '    def f(self):\n'
            '        super().f()\n',
        ),
        (
            'class Outer:\n'
            '    class C(Base):\n'
            '        def f(self):\n'
            '            super (C, self).f()\n',
            'class Outer:\n'
            '    class C(Base):\n'
            '        def f(self):\n'
            '            super().f()\n',
        ),
        (
            'class C(Base):\n'
            '    f = lambda self: super(C, self).f()\n',
            'class C(Base):\n'
            '    f = lambda self: super().f()\n',
        ),
        (
            'class C(Base):\n'
            '    @classmethod\n'
            '    def f(cls):\n'
            '        super(C, cls).f()\n',
            'class C(Base):\n'
            '    @classmethod\n'
            '    def f(cls):\n'
            '        super().f()\n',
        ),
        pytest.param(
            'class C:\n'
            '    async def foo(self):\n'
            '        super(C, self).foo()\n',

            'class C:\n'
            '    async def foo(self):\n'
            '        super().foo()\n',

            id='async def super',
        ),
    ),
)
def test_fix_super(s, expected):
    assert _fix_plugins(s, settings=Settings(min_version=(3,))) == expected


@pytest.mark.parametrize(
    's',
    (
        pytest.param(
            'class C(B):\n'
            '    def f(self):\n'
            '        B.f(notself)\n',
            id='old style super, first argument is not first function arg',
        ),
        pytest.param(
            'class C(B1, B2):\n'
            '    def f(self):\n'
            '        B1.f(self)\n',
            # TODO: is this safe to rewrite? I don't think so
            id='old-style super, multiple inheritance first class',
        ),
        pytest.param(
            'class C(B1, B2):\n'
            '    def f(self):\n'
            '        B2.f(self)\n',
            # TODO: is this safe to rewrite? I don't think so
            id='old-style super, multiple inheritance not-first class',
        ),
        pytest.param(
            'class C(Base):\n'
            '    def f(self):\n'
            '        return [Base.f(self) for _ in ()]\n',
            id='super in comprehension',
        ),
        pytest.param(
            'class C(Base):\n'
            '    def f(self):\n'
            '        def g():\n'
            '            Base.f(self)\n'
            '        g()\n',
            id='super in nested functions',
        ),
        pytest.param(
            'class C(not_simple()):\n'
            '    def f(self):\n'
            '        not_simple().f(self)\n',
            id='not a simple base',
        ),
        pytest.param(
            'class C(a().b):\n'
            '    def f(self):\n'
            '        a().b.f(self)\n',
            id='non simple attribute base',
        ),
        pytest.param(
            'class C:\n'
            '    @classmethod\n'
            '    def make(cls, instance):\n'
            '        ...\n'
            'class D(C):\n'
            '   def find(self):\n'
            '        return C.make(self)\n',
        ),
    ),
)
def test_old_style_class_super_noop(s):
    assert _fix_plugins(s, settings=Settings(min_version=(3,))) == s


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        (
            'class C(B):\n'
            '    def f(self):\n'
            '        B.f(self)\n'
            '        B.f(self, arg, arg)\n',
            'class C(B):\n'
            '    def f(self):\n'
            '        super().f()\n'
            '        super().f(arg, arg)\n',
        ),
        pytest.param(
            'class C(B):\n'
            '    def f(self, a):\n'
            '         B.f(\n'
            '             self,\n'
            '             a,\n'
            '         )\n',

            'class C(B):\n'
            '    def f(self, a):\n'
            '         super().f(\n'
            '             a,\n'
            '         )\n',
            id='multi-line super call',
        ),
    ),
)
def test_old_style_class_super(s, expected):
    assert _fix_plugins(s, settings=Settings(min_version=(3,))) == expected
