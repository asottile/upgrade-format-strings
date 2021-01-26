import pytest

from pyupgrade import _fix_py36_plus


@pytest.mark.parametrize(
    's',
    (
        # syntax error
        '(',
        # invalid format strings
        "'{'.format(a)", "'}'.format(a)",
        # weird syntax
        '"{}" . format(x)',
        # spans multiple lines
        '"{}".format(\n    a,\n)',
        # starargs
        '"{} {}".format(*a)', '"{foo} {bar}".format(**b)"',
        # likely makes the format longer
        '"{0} {0}".format(arg)', '"{x} {x}".format(arg)',
        '"{x.y} {x.z}".format(arg)',
        # bytestrings don't participate in `.format()` or `f''`
        # but are legal in python 2
        'b"{} {}".format(a, b)',
        # for now, too difficult to rewrite correctly
        '"{:{}}".format(x, y)',
        '"{a[b]}".format(a=a)',
        '"{a.a[b]}".format(a=a)',
        # not enough placeholders / placeholders missing
        '"{}{}".format(a)', '"{a}{b}".format(a=a)',
        # invald \N escape
        r'"{} \\N{snowman}".format(a)',
    ),
)
def test_fix_fstrings_noop(s):
    assert _fix_py36_plus(s) == s


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        ('"{} {}".format(a, b)', 'f"{a} {b}"'),
        ('"{1} {0}".format(a, b)', 'f"{b} {a}"'),
        ('"{x.y}".format(x=z)', 'f"{z.y}"'),
        ('"{.x} {.y}".format(a, b)', 'f"{a.x} {b.y}"'),
        ('"{} {}".format(a.b, c.d)', 'f"{a.b} {c.d}"'),
        ('"{}".format(a())', 'f"{a()}"'),
        ('"{}".format(a.b())', 'f"{a.b()}"'),
        ('"{}".format(a.b().c())', 'f"{a.b().c()}"'),
        ('"hello {}!".format(name)', 'f"hello {name}!"'),
        ('"{}{{}}{}".format(escaped, y)', 'f"{escaped}{{}}{y}"'),
        ('"{}{b}{}".format(a, c, b=b)', 'f"{a}{b}{c}"'),
        (r'"{} \N{snowman}".format(a)', r'f"{a} \N{snowman}"'),
        (r'"\\N{snowman}".format(snowman=snowman)', r'f"\\N{snowman}"'),
        # TODO: poor man's f-strings?
        # '"{foo}".format(**locals())'
    ),
)
def test_fix_fstrings(s, expected):
    assert _fix_py36_plus(s) == expected
