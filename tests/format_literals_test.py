import pytest

from pyupgrade import _fix_tokens
from pyupgrade import parse_format
from pyupgrade import unparse_parsed_string


@pytest.mark.parametrize(
    's',
    (
        '', 'foo', '{}', '{0}', '{named}', '{!r}', '{:>5}', '{{', '}}',
        '{0!s:15}',
    ),
)
def test_roundtrip_text(s):
    assert unparse_parsed_string(parse_format(s)) == s


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        ('{:}', '{}'),
        ('{0:}', '{0}'),
        ('{0!r:}', '{0!r}'),
    ),
)
def test_intentionally_not_round_trip(s, expected):
    # Our unparse simplifies empty parts, whereas stdlib allows them
    ret = unparse_parsed_string(parse_format(s))
    assert ret == expected


@pytest.mark.parametrize(
    's',
    (
        # Don't touch syntax errors
        '"{0}"format(1)',
        # Don't touch py27 format strings
        "'{}'.format(1)",
        # Don't touch invalid format strings
        "'{'.format(1)", "'}'.format(1)",
        # Don't touch non-format strings
        "x = ('{0} {1}',)\n",
        # Don't touch non-incrementing integers
        "'{0} {0}'.format(1)",
        # Formats can be embedded in formats, leave these alone?
        "'{0:<{1}}'.format(1, 4)",
        # don't attempt to fix this, garbage in garbage out
        "'{' '0}'.format(1)",
        # comment looks like placeholder but is not!
        '("{0}" # {1}\n"{2}").format(1, 2, 3)',
        # this works by accident (extended escape treated as placeholder)
        r'"\N{snowman} {}".format(1)',
        # don't touch f-strings (these are wrong but don't make it worse)
        'f"{0}".format(a)',
    ),
)
def test_format_literals_noop(s):
    assert _fix_tokens(s, min_version=(2, 7)) == s


@pytest.mark.parametrize(
    ('s', 'expected'),
    (
        # Simplest case
        ("'{0}'.format(1)", "'{}'.format(1)"),
        ("'{0:x}'.format(30)", "'{:x}'.format(30)"),
        ("x = '{0}'.format(1)", "x = '{}'.format(1)"),
        # Multiline strings
        ("'''{0}\n{1}\n'''.format(1, 2)", "'''{}\n{}\n'''.format(1, 2)"),
        # Multiple implicitly-joined strings
        ("'{0}' '{1}'.format(1, 2)", "'{}' '{}'.format(1, 2)"),
        # Multiple implicitly-joined strings over lines
        (
            'print(\n'
            "    'foo{0}'\n"
            "    'bar{1}'.format(1, 2)\n"
            ')',
            'print(\n'
            "    'foo{}'\n"
            "    'bar{}'.format(1, 2)\n"
            ')',
        ),
        # Multiple implicitly-joind strings over lines with comments
        (
            'print(\n'
            "    'foo{0}'  # ohai\n"
            "    'bar{1}'.format(1, 2)\n"
            ')',
            'print(\n'
            "    'foo{}'  # ohai\n"
            "    'bar{}'.format(1, 2)\n"
            ')',
        ),
        # joined by backslash
        (
            'x = "foo {0}" \\\n'
            '    "bar {1}".format(1, 2)',
            'x = "foo {}" \\\n'
            '    "bar {}".format(1, 2)',
        ),
        # parenthesized string literals
        ('("{0}").format(1)', '("{}").format(1)'),
        # extended unicode escapes
        (r'"\N{snowman} {0}".format(1)', r'"\N{snowman} {}".format(1)'),
    ),
)
def test_format_literals(s, expected):
    assert _fix_tokens(s, min_version=(2, 7)) == expected
