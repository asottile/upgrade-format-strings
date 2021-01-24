import ast
import keyword
import sys
from typing import List
from typing import NamedTuple
from typing import Optional

from tokenize_rt import NON_CODING_TOKENS
from tokenize_rt import Token
from tokenize_rt import UNIMPORTANT_WS

BRACES = {'(': ')', '[': ']', '{': '}'}
OPENING, CLOSING = frozenset(BRACES), frozenset(BRACES.values())
KEYWORDS = frozenset(keyword.kwlist)


def immediately_paren(func: str, tokens: List[Token], i: int) -> bool:
    return tokens[i].src == func and tokens[i + 1].src == '('


class Victims(NamedTuple):
    starts: List[int]
    ends: List[int]
    first_comma_index: Optional[int]
    arg_index: int


def _search_until(tokens: List[Token], idx: int, arg: ast.expr) -> int:
    while (
            idx < len(tokens) and
            not (
                tokens[idx].line == arg.lineno and
                tokens[idx].utf8_byte_offset == arg.col_offset
            )
    ):
        idx += 1
    return idx


def find_token(tokens: List[Token], i: int, src: str) -> int:
    while tokens[i].src != src:
        i += 1
    return i


def find_open_paren(tokens: List[Token], i: int) -> int:
    return find_token(tokens, i, '(')


if sys.version_info >= (3, 8):  # pragma: no cover (py38+)
    # python 3.8 fixed the offsets of generators / tuples
    def _arg_token_index(tokens: List[Token], i: int, arg: ast.expr) -> int:
        idx = _search_until(tokens, i, arg) + 1
        while idx < len(tokens) and tokens[idx].name in NON_CODING_TOKENS:
            idx += 1
        return idx
else:  # pragma: no cover (<py38)
    def _arg_token_index(tokens: List[Token], i: int, arg: ast.expr) -> int:
        # lists containing non-tuples report the first element correctly
        if isinstance(arg, ast.List):
            # If the first element is a tuple, the ast lies to us about its col
            # offset.  We must find the first `(` token after the start of the
            # list element.
            if isinstance(arg.elts[0], ast.Tuple):
                i = _search_until(tokens, i, arg)
                return find_open_paren(tokens, i)
            else:
                return _search_until(tokens, i, arg.elts[0])
            # others' start position points at their first child node already
        else:
            return _search_until(tokens, i, arg)


def victims(
        tokens: List[Token],
        start: int,
        arg: ast.expr,
        gen: bool,
) -> Victims:
    starts = [start]
    start_depths = [1]
    ends: List[int] = []
    first_comma_index = None
    arg_depth = None
    arg_index = _arg_token_index(tokens, start, arg)
    brace_stack = [tokens[start].src]
    i = start + 1

    while brace_stack:
        token = tokens[i].src
        is_start_brace = token in BRACES
        is_end_brace = token == BRACES[brace_stack[-1]]

        if i == arg_index:
            arg_depth = len(brace_stack)

        if is_start_brace:
            brace_stack.append(token)

        # Remove all braces before the first element of the inner
        # comprehension's target.
        if is_start_brace and arg_depth is None:
            start_depths.append(len(brace_stack))
            starts.append(i)

        if (
                token == ',' and
                len(brace_stack) == arg_depth and
                first_comma_index is None
        ):
            first_comma_index = i

        if is_end_brace and len(brace_stack) in start_depths:
            if tokens[i - 2].src == ',' and tokens[i - 1].src == ' ':
                ends.extend((i - 2, i - 1, i))
            elif tokens[i - 1].src == ',':
                ends.extend((i - 1, i))
            else:
                ends.append(i)
            if len(brace_stack) > 1 and tokens[i + 1].src == ',':
                ends.append(i + 1)

        if is_end_brace:
            brace_stack.pop()

        i += 1
    # May need to remove a trailing comma for a comprehension
    if gen:
        i -= 2
        while tokens[i].name in NON_CODING_TOKENS:
            i -= 1
        if tokens[i].src == ',':
            ends.append(i)

    return Victims(starts, sorted(set(ends)), first_comma_index, arg_index)


def _is_on_a_line_by_self(tokens: List[Token], i: int) -> bool:
    return (
        tokens[i - 2].name == 'NL' and
        tokens[i - 1].name == UNIMPORTANT_WS and
        tokens[i + 1].name == 'NL'
    )


def remove_brace(tokens: List[Token], i: int) -> None:
    if _is_on_a_line_by_self(tokens, i):
        del tokens[i - 1:i + 2]
    else:
        del tokens[i]