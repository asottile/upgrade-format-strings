import ast
import collections
import contextlib
import functools
from typing import Dict
from typing import Generator
from typing import Iterable
from typing import List
from typing import Set
from typing import Tuple

from tokenize_rt import Offset
from tokenize_rt import Token
from tokenize_rt import tokens_to_src

from pyupgrade._ast_helpers import ast_to_offset
from pyupgrade._ast_helpers import targets_same
from pyupgrade._data import register
from pyupgrade._data import State
from pyupgrade._data import TokenFunc
from pyupgrade._token_helpers import Block
from pyupgrade._token_helpers import find_and_replace_call
from pyupgrade._token_helpers import find_block_start
from pyupgrade._token_helpers import find_open_paren
from pyupgrade._token_helpers import find_token
from pyupgrade._token_helpers import parse_call_args

FUNC_TYPES = (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)
NON_LAMBDA_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _fix_yield(i: int, tokens: List[Token]) -> None:
    in_token = find_token(tokens, i, 'in')
    colon = find_block_start(tokens, i)
    block = Block.find(tokens, i, trim_end=True)
    container = tokens_to_src(tokens[in_token + 1:colon]).strip()
    tokens[i:block.end] = [Token('CODE', f'yield from {container}\n')]


def _fix_old_super(i: int, tokens: List[Token]) -> None:
    j = find_open_paren(tokens, i)
    k = j - 1
    while tokens[k].src != '.':
        k -= 1
    func_args, end = parse_call_args(tokens, j)
    # remove the first argument
    if len(func_args) == 1:
        del tokens[func_args[0][0]:func_args[0][0] + 1]
    else:
        del tokens[func_args[0][0]:func_args[1][0] + 1]
    tokens[i:k] = [Token('CODE', 'super()')]


def _is_simple_base(base: ast.AST) -> bool:
    return (
        isinstance(base, ast.Name) or (
            isinstance(base, ast.Attribute) and
            _is_simple_base(base.value)
        )
    )


class Scope:
    def __init__(self, node: ast.AST) -> None:
        self.node = node

        self.reads: Set[str] = set()
        self.writes: Set[str] = set()

        self.yield_from_fors: Set[Offset] = set()
        self.yield_from_names: Dict[str, Set[Offset]]
        self.yield_from_names = collections.defaultdict(set)


class Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scopes: List[Scope] = []
        self.super_offsets: Set[Offset] = set()
        self.old_super_offsets: Set[Offset] = set()
        self.yield_offsets: Set[Offset] = set()

    @contextlib.contextmanager
    def _scope(self, node: ast.AST) -> Generator[None, None, None]:
        self._scopes.append(Scope(node))
        try:
            yield
        finally:
            info = self._scopes.pop()
            # discard any that were referenced outside of the loop
            for name in info.reads:
                offsets = info.yield_from_names[name]
                info.yield_from_fors.difference_update(offsets)
            self.yield_offsets.update(info.yield_from_fors)
            if self._scopes:
                cell_reads = info.reads - info.writes
                self._scopes[-1].reads.update(cell_reads)

    def _visit_scope(self, node: ast.AST) -> None:
        with self._scope(node):
            self.generic_visit(node)

    visit_ClassDef = _visit_scope
    visit_Lambda = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope
    visit_ListComp = visit_SetComp = _visit_scope
    visit_DictComp = visit_GeneratorExp = _visit_scope

    def visit_Name(self, node: ast.Name) -> None:
        if self._scopes:
            if isinstance(node.ctx, ast.Load):
                self._scopes[-1].reads.add(node.id)
            elif isinstance(node.ctx, (ast.Store, ast.Del)):
                self._scopes[-1].writes.add(node.id)
            else:
                raise AssertionError(node)

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
                isinstance(node.func, ast.Name) and
                node.func.id == 'super' and
                len(node.args) == 2 and
                isinstance(node.args[0], ast.Name) and
                isinstance(node.args[1], ast.Name) and
                len(self._scopes) >= 2 and
                # the second to last scope is the class in arg1
                isinstance(self._scopes[-2].node, ast.ClassDef) and
                node.args[0].id == self._scopes[-2].node.name and
                # the last scope is a function where the first arg is arg2
                isinstance(self._scopes[-1].node, FUNC_TYPES) and
                self._scopes[-1].node.args.args and
                node.args[1].id == self._scopes[-1].node.args.args[0].arg
        ):
            self.super_offsets.add(ast_to_offset(node))
        elif (
                # base.funcname(funcarg1, ...)
                isinstance(node.func, ast.Attribute) and
                len(node.args) >= 1 and
                isinstance(node.args[0], ast.Name) and
                len(self._scopes) >= 2 and
                # last stack is a function whose first argument is the first
                # argument of this function
                isinstance(self._scopes[-1].node, NON_LAMBDA_FUNC_TYPES) and
                node.func.attr == self._scopes[-1].node.name and
                len(self._scopes[-1].node.args.args) >= 1 and
                node.args[0].id == self._scopes[-1].node.args.args[0].arg and
                # the function is an attribute of the contained class name
                isinstance(self._scopes[-2].node, ast.ClassDef) and
                len(self._scopes[-2].node.bases) == 1 and
                _is_simple_base(self._scopes[-2].node.bases[0]) and
                targets_same(
                    self._scopes[-2].node.bases[0],
                    node.func.value,
                )
        ):
            self.old_super_offsets.add(ast_to_offset(node))

        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        if (
            len(self._scopes) >= 1 and
            not isinstance(self._scopes[-1].node, ast.AsyncFunctionDef) and
            len(node.body) == 1 and
            isinstance(node.body[0], ast.Expr) and
            isinstance(node.body[0].value, ast.Yield) and
            node.body[0].value.value is not None and
            targets_same(node.target, node.body[0].value.value) and
            not node.orelse
        ):
            offset = ast_to_offset(node)
            func_info = self._scopes[-1]
            func_info.yield_from_fors.add(offset)
            for target_node in ast.walk(node.target):
                if (
                        isinstance(target_node, ast.Name) and
                        isinstance(target_node.ctx, ast.Store)
                ):
                    func_info.yield_from_names[target_node.id].add(offset)
            # manually visit, but with target+body as a separate scope
            self.visit(node.iter)
            with self._scope(node):
                self.visit(node.target)
                for stmt in node.body:
                    self.visit(stmt)
                assert not node.orelse
        else:
            self.generic_visit(node)


@register(ast.Module)
def visit_Module(
        state: State,
        node: ast.Module,
        parent: ast.AST,
) -> Iterable[Tuple[Offset, TokenFunc]]:
    if state.settings.min_version < (3,):
        return

    visitor = Visitor()
    visitor.visit(node)

    super_func = functools.partial(find_and_replace_call, template='super()')
    for offset in visitor.super_offsets:
        yield offset, super_func

    for offset in visitor.old_super_offsets:
        yield offset, _fix_old_super

    for offset in visitor.yield_offsets:
        yield offset, _fix_yield
