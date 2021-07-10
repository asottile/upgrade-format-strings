import ast
import warnings
from typing import Any
from typing import Container
from typing import Dict
from typing import Iterable
from typing import Set
from typing import Tuple
from typing import Type
from typing import Union

from tokenize_rt import Offset


def ast_parse(contents_text: str) -> ast.Module:
    # intentionally ignore warnings, we might be fixing warning-ridden syntax
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return ast.parse(contents_text.encode())


def ast_to_offset(node: Union[ast.expr, ast.stmt]) -> Offset:
    return Offset(node.lineno, node.col_offset)


def is_name_attr(
        node: ast.AST,
        imports: Dict[str, Set[str]],
        mod: str,
        names: Container[str],
) -> bool:
    return (
        isinstance(node, ast.Name) and
        node.id in names and
        node.id in imports[mod]
    ) or (
        isinstance(node, ast.Attribute) and
        isinstance(node.value, ast.Name) and
        node.value.id == mod and
        node.attr in names
    )


def has_starargs(call: ast.Call) -> bool:
    return (
        any(k.arg is None for k in call.keywords) or
        any(isinstance(a, ast.Starred) for a in call.args)
    )


def _all_isinstance(
        vals: Iterable[Any],
        tp: Union[Type[Any], Tuple[Type[Any], ...]],
) -> bool:
    return all(isinstance(v, tp) for v in vals)


def _fields_same(n1: ast.AST, n2: ast.AST) -> bool:
    for (a1, v1), (a2, v2) in zip(ast.iter_fields(n1), ast.iter_fields(n2)):
        # ignore ast attributes, they'll be covered by walk
        if a1 != a2:
            return False
        elif _all_isinstance((v1, v2), ast.AST):
            continue
        elif _all_isinstance((v1, v2), (list, tuple)):
            if len(v1) != len(v2):
                return False
            # ignore sequences which are all-ast, they'll be covered by walk
            elif _all_isinstance(v1, ast.AST) and _all_isinstance(v2, ast.AST):
                continue
            elif v1 != v2:
                return False
        elif v1 != v2:
            return False
    return True


def targets_same(node1: ast.AST, node2: ast.AST) -> bool:
    for t1, t2 in zip(ast.walk(node1), ast.walk(node2)):
        # ignore `ast.Load` / `ast.Store`
        if _all_isinstance((t1, t2), ast.expr_context):
            continue
        elif type(t1) != type(t2):
            return False
        elif not _fields_same(t1, t2):
            return False
    else:
        return True
