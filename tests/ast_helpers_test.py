import ast

from pyupgrade._ast_helpers import _fields_same
from pyupgrade._ast_helpers import targets_same


def test_targets_same():
    assert targets_same(ast.parse('global a, b'), ast.parse('global a, b'))
    assert not targets_same(ast.parse('global a'), ast.parse('global b'))


def _get_body(expr):
    body = ast.parse(expr).body[0]
    assert isinstance(body, ast.Expr)
    return body.value


def test_fields_same():
    assert not _fields_same(_get_body('x'), _get_body('1'))
