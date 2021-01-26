import argparse
import ast
import codecs
import collections
import contextlib
import keyword
import re
import string
import sys
import tokenize
import warnings
from typing import Any
from typing import cast
from typing import Container
from typing import Dict
from typing import Generator
from typing import Iterable
from typing import List
from typing import Match
from typing import NamedTuple
from typing import Optional
from typing import Pattern
from typing import Sequence
from typing import Set
from typing import Tuple
from typing import Type
from typing import Union

from tokenize_rt import NON_CODING_TOKENS
from tokenize_rt import Offset
from tokenize_rt import parse_string_literal
from tokenize_rt import reversed_enumerate
from tokenize_rt import rfind_string_parts
from tokenize_rt import src_to_tokens
from tokenize_rt import Token
from tokenize_rt import tokens_to_src
from tokenize_rt import UNIMPORTANT_WS

MinVersion = Tuple[int, ...]
DotFormatPart = Tuple[str, Optional[str], Optional[str], Optional[str]]
PercentFormatPart = Tuple[
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[str],
    str,
]
PercentFormat = Tuple[str, Optional[PercentFormatPart]]

ListCompOrGeneratorExp = Union[ast.ListComp, ast.GeneratorExp]
ListOrTuple = Union[ast.List, ast.Tuple]
NameOrAttr = Union[ast.Name, ast.Attribute]
AnyFunctionDef = Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda]
SyncFunctionDef = Union[ast.FunctionDef, ast.Lambda]

_stdlib_parse_format = string.Formatter().parse

_KEYWORDS = frozenset(keyword.kwlist)

_EXPR_NEEDS_PARENS: Tuple[Type[ast.expr], ...] = (
    ast.Await, ast.BinOp, ast.BoolOp, ast.Compare, ast.GeneratorExp, ast.IfExp,
    ast.Lambda, ast.UnaryOp,
)
if sys.version_info >= (3, 8):  # pragma: no cover (py38+)
    _EXPR_NEEDS_PARENS += (ast.NamedExpr,)


def parse_format(s: str) -> Tuple[DotFormatPart, ...]:
    """Makes the empty string not a special case.  In the stdlib, there's
    loss of information (the type) on the empty string.
    """
    parsed = tuple(_stdlib_parse_format(s))
    if not parsed:
        return ((s, None, None, None),)
    collapse_part = None
    new_parsed = []
    for part in parsed:
        fragment, fmtkey, _, _ = part
        if collapse_part:
            fragment = collapse_part[0] + fragment
            part = (fragment,) + part[1:]
            collapse_part = None
        # rebuild \N escape seqs
        if ENDS_ON_NAMED_UNICODE_RE.search(fragment) and fmtkey:
            collapse_part = (fragment + '{' + fmtkey + '}', None, None, None)
        if collapse_part is None:
            new_parsed.append(part)
    if collapse_part:
        new_parsed.append(collapse_part)
    return tuple(new_parsed)


def unparse_parsed_string(parsed: Sequence[DotFormatPart]) -> str:
    def _convert_tup(tup: DotFormatPart) -> str:
        ret, field_name, format_spec, conversion = tup
        ret = ret.replace('{', '{{')
        ret = ret.replace('}', '}}')
        if r'\N' in ret:
            # Convert \N{{snowman}} back to \N{snowman}
            ret = NAMED_UNICODE_RE.sub(r'\\N{\1}', ret)
        if field_name is not None:
            ret += '{' + field_name
            if conversion:
                ret += '!' + conversion
            if format_spec:
                ret += ':' + format_spec
            ret += '}'
        return ret

    return ''.join(_convert_tup(tup) for tup in parsed)


def _ast_to_offset(node: Union[ast.expr, ast.stmt]) -> Offset:
    return Offset(node.lineno, node.col_offset)


def ast_parse(contents_text: str) -> ast.Module:
    # intentionally ignore warnings, we might be fixing warning-ridden syntax
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return ast.parse(contents_text.encode())


def inty(s: str) -> bool:
    try:
        int(s)
        return True
    except (ValueError, TypeError):
        return False


BRACES = {'(': ')', '[': ']', '{': '}'}
OPENING, CLOSING = frozenset(BRACES), frozenset(BRACES.values())
SET_TRANSFORM = (ast.List, ast.ListComp, ast.GeneratorExp, ast.Tuple)


def _is_wtf(func: str, tokens: List[Token], i: int) -> bool:
    return tokens[i].src != func or tokens[i + 1].src != '('


def _process_set_empty_literal(tokens: List[Token], start: int) -> None:
    if _is_wtf('set', tokens, start):
        return

    i = start + 2
    brace_stack = ['(']
    while brace_stack:
        token = tokens[i].src
        if token == BRACES[brace_stack[-1]]:
            brace_stack.pop()
        elif token in BRACES:
            brace_stack.append(token)
        elif '\n' in token:
            # Contains a newline, could cause a SyntaxError, bail
            return
        i += 1

    # Remove the inner tokens
    del tokens[start + 2:i - 1]


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
                return _find_open_paren(tokens, i)
            else:
                return _search_until(tokens, i, arg.elts[0])
            # others' start position points at their first child node already
        else:
            return _search_until(tokens, i, arg)


class Victims(NamedTuple):
    starts: List[int]
    ends: List[int]
    first_comma_index: Optional[int]
    arg_index: int


def _victims(
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


def _find_token(tokens: List[Token], i: int, src: str) -> int:
    while tokens[i].src != src:
        i += 1
    return i


def _find_open_paren(tokens: List[Token], i: int) -> int:
    return _find_token(tokens, i, '(')


def _is_on_a_line_by_self(tokens: List[Token], i: int) -> bool:
    return (
        tokens[i - 2].name == 'NL' and
        tokens[i - 1].name == UNIMPORTANT_WS and
        tokens[i + 1].name == 'NL'
    )


def _remove_brace(tokens: List[Token], i: int) -> None:
    if _is_on_a_line_by_self(tokens, i):
        del tokens[i - 1:i + 2]
    else:
        del tokens[i]


def _process_set_literal(
        tokens: List[Token],
        start: int,
        arg: ast.expr,
) -> None:
    if _is_wtf('set', tokens, start):
        return

    gen = isinstance(arg, ast.GeneratorExp)
    set_victims = _victims(tokens, start + 1, arg, gen=gen)

    del set_victims.starts[0]
    end_index = set_victims.ends.pop()

    tokens[end_index] = Token('OP', '}')
    for index in reversed(set_victims.starts + set_victims.ends):
        _remove_brace(tokens, index)
    tokens[start:start + 2] = [Token('OP', '{')]


def _process_dict_comp(
        tokens: List[Token],
        start: int,
        arg: ListCompOrGeneratorExp,
) -> None:
    if _is_wtf('dict', tokens, start):
        return

    dict_victims = _victims(tokens, start + 1, arg, gen=True)
    elt_victims = _victims(tokens, dict_victims.arg_index, arg.elt, gen=True)

    del dict_victims.starts[0]
    end_index = dict_victims.ends.pop()

    tokens[end_index] = Token('OP', '}')
    for index in reversed(dict_victims.ends):
        _remove_brace(tokens, index)
    # See #6, Fix SyntaxError from rewriting dict((a, b)for a, b in y)
    if tokens[elt_victims.ends[-1] + 1].src == 'for':
        tokens.insert(elt_victims.ends[-1] + 1, Token(UNIMPORTANT_WS, ' '))
    for index in reversed(elt_victims.ends):
        _remove_brace(tokens, index)
    assert elt_victims.first_comma_index is not None
    tokens[elt_victims.first_comma_index] = Token('OP', ':')
    for index in reversed(dict_victims.starts + elt_victims.starts):
        _remove_brace(tokens, index)
    tokens[start:start + 2] = [Token('OP', '{')]


def _process_is_literal(
        tokens: List[Token],
        i: int,
        compare: Union[ast.Is, ast.IsNot],
) -> None:
    while tokens[i].src != 'is':
        i -= 1
    if isinstance(compare, ast.Is):
        tokens[i] = tokens[i]._replace(src='==')
    else:
        tokens[i] = tokens[i]._replace(src='!=')
        # since we iterate backward, the dummy tokens keep the same length
        i += 1
        while tokens[i].src != 'not':
            tokens[i] = Token('DUMMY', '')
            i += 1
        tokens[i] = Token('DUMMY', '')


LITERAL_TYPES = (ast.Str, ast.Num, ast.Bytes)


class Py2CompatibleVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.dicts: Dict[Offset, ListCompOrGeneratorExp] = {}
        self.sets: Dict[Offset, ast.expr] = {}
        self.set_empty_literals: Dict[Offset, ListOrTuple] = {}
        self.is_literal: Dict[Offset, Union[ast.Is, ast.IsNot]] = {}

    def visit_Call(self, node: ast.Call) -> None:
        if (
                isinstance(node.func, ast.Name) and
                node.func.id == 'set' and
                len(node.args) == 1 and
                not node.keywords and
                isinstance(node.args[0], SET_TRANSFORM)
        ):
            arg, = node.args
            key = _ast_to_offset(node.func)
            if isinstance(arg, (ast.List, ast.Tuple)) and not arg.elts:
                self.set_empty_literals[key] = arg
            else:
                self.sets[key] = arg
        elif (
                isinstance(node.func, ast.Name) and
                node.func.id == 'dict' and
                len(node.args) == 1 and
                not node.keywords and
                isinstance(node.args[0], (ast.ListComp, ast.GeneratorExp)) and
                isinstance(node.args[0].elt, (ast.Tuple, ast.List)) and
                len(node.args[0].elt.elts) == 2
        ):
            self.dicts[_ast_to_offset(node.func)] = node.args[0]
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        left = node.left
        for op, right in zip(node.ops, node.comparators):
            if (
                    isinstance(op, (ast.Is, ast.IsNot)) and
                    (
                        isinstance(left, LITERAL_TYPES) or
                        isinstance(right, LITERAL_TYPES)
                    )
            ):
                self.is_literal[_ast_to_offset(right)] = op
            left = right

        self.generic_visit(node)


def _fix_py2_compatible(contents_text: str) -> str:
    try:
        ast_obj = ast_parse(contents_text)
    except SyntaxError:
        return contents_text
    visitor = Py2CompatibleVisitor()
    visitor.visit(ast_obj)
    if not any((
            visitor.dicts,
            visitor.sets,
            visitor.set_empty_literals,
            visitor.is_literal,
    )):
        return contents_text

    try:
        tokens = src_to_tokens(contents_text)
    except tokenize.TokenError:  # pragma: no cover (bpo-2180)
        return contents_text
    for i, token in reversed_enumerate(tokens):
        if token.offset in visitor.dicts:
            _process_dict_comp(tokens, i, visitor.dicts[token.offset])
        elif token.offset in visitor.set_empty_literals:
            _process_set_empty_literal(tokens, i)
        elif token.offset in visitor.sets:
            _process_set_literal(tokens, i, visitor.sets[token.offset])
        elif token.offset in visitor.is_literal:
            _process_is_literal(tokens, i, visitor.is_literal[token.offset])
    return tokens_to_src(tokens)


def _imports_future(contents_text: str, future_name: str) -> bool:
    try:
        ast_obj = ast_parse(contents_text)
    except SyntaxError:
        return False

    for node in ast_obj.body:
        # Docstring
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Str):
            continue
        elif isinstance(node, ast.ImportFrom):
            if (
                node.level == 0 and
                node.module == '__future__' and
                any(name.name == future_name for name in node.names)
            ):
                return True
            elif node.module == '__future__':
                continue
            else:
                return False
        else:
            return False

    return False


# https://docs.python.org/3/reference/lexical_analysis.html
ESCAPE_STARTS = frozenset((
    '\n', '\r', '\\', "'", '"', 'a', 'b', 'f', 'n', 'r', 't', 'v',
    '0', '1', '2', '3', '4', '5', '6', '7',  # octal escapes
    'x',  # hex escapes
))
ESCAPE_RE = re.compile(r'\\.', re.DOTALL)
NAMED_ESCAPE_NAME = re.compile(r'\{[^}]+\}')


def _fix_escape_sequences(token: Token) -> Token:
    prefix, rest = parse_string_literal(token.src)
    actual_prefix = prefix.lower()

    if 'r' in actual_prefix or '\\' not in rest:
        return token

    is_bytestring = 'b' in actual_prefix

    def _is_valid_escape(match: Match[str]) -> bool:
        c = match.group()[1]
        return (
            c in ESCAPE_STARTS or
            (not is_bytestring and c in 'uU') or
            (
                not is_bytestring and
                c == 'N' and
                bool(NAMED_ESCAPE_NAME.match(rest, match.end()))
            )
        )

    has_valid_escapes = False
    has_invalid_escapes = False
    for match in ESCAPE_RE.finditer(rest):
        if _is_valid_escape(match):
            has_valid_escapes = True
        else:
            has_invalid_escapes = True

    def cb(match: Match[str]) -> str:
        matched = match.group()
        if _is_valid_escape(match):
            return matched
        else:
            return fr'\{matched}'

    if has_invalid_escapes and (has_valid_escapes or 'u' in actual_prefix):
        return token._replace(src=prefix + ESCAPE_RE.sub(cb, rest))
    elif has_invalid_escapes and not has_valid_escapes:
        return token._replace(src=prefix + 'r' + rest)
    else:
        return token


def _remove_u_prefix(token: Token) -> Token:
    prefix, rest = parse_string_literal(token.src)
    if 'u' not in prefix.lower():
        return token
    else:
        new_prefix = prefix.replace('u', '').replace('U', '')
        return token._replace(src=new_prefix + rest)


def _fix_ur_literals(token: Token) -> Token:
    prefix, rest = parse_string_literal(token.src)
    if prefix.lower() != 'ur':
        return token
    else:
        def cb(match: Match[str]) -> str:
            escape = match.group()
            if escape[1].lower() == 'u':
                return escape
            else:
                return '\\' + match.group()

        rest = ESCAPE_RE.sub(cb, rest)
        prefix = prefix.replace('r', '').replace('R', '')
        return token._replace(src=prefix + rest)


def _fix_long(src: str) -> str:
    return src.rstrip('lL')


def _fix_octal(s: str) -> str:
    if not s.startswith('0') or not s.isdigit() or s == len(s) * '0':
        return s
    elif len(s) == 2:
        return s[1:]
    else:
        return '0o' + s[1:]


def _fix_extraneous_parens(tokens: List[Token], i: int) -> None:
    # search forward for another non-coding token
    i += 1
    while tokens[i].name in NON_CODING_TOKENS:
        i += 1
    # if we did not find another brace, return immediately
    if tokens[i].src != '(':
        return

    start = i
    depth = 1
    while depth:
        i += 1
        # found comma or yield at depth 1: this is a tuple / coroutine
        if depth == 1 and tokens[i].src in {',', 'yield'}:
            return
        elif tokens[i].src in OPENING:
            depth += 1
        elif tokens[i].src in CLOSING:
            depth -= 1
    end = i

    # empty tuple
    if all(t.name in NON_CODING_TOKENS for t in tokens[start + 1:i]):
        return

    # search forward for the next non-coding token
    i += 1
    while tokens[i].name in NON_CODING_TOKENS:
        i += 1

    if tokens[i].src == ')':
        _remove_brace(tokens, end)
        _remove_brace(tokens, start)


def _remove_fmt(tup: DotFormatPart) -> DotFormatPart:
    if tup[1] is None:
        return tup
    else:
        return (tup[0], '', tup[2], tup[3])


def _fix_format_literal(tokens: List[Token], end: int) -> None:
    parts = rfind_string_parts(tokens, end)
    parsed_parts = []
    last_int = -1
    for i in parts:
        # f'foo {0}'.format(...) would get turned into a SyntaxError
        prefix, _ = parse_string_literal(tokens[i].src)
        if 'f' in prefix.lower():
            return

        try:
            parsed = parse_format(tokens[i].src)
        except ValueError:
            # the format literal was malformed, skip it
            return

        # The last segment will always be the end of the string and not a
        # format, slice avoids the `None` format key
        for _, fmtkey, spec, _ in parsed[:-1]:
            if (
                    fmtkey is not None and inty(fmtkey) and
                    int(fmtkey) == last_int + 1 and
                    spec is not None and '{' not in spec
            ):
                last_int += 1
            else:
                return

        parsed_parts.append(tuple(_remove_fmt(tup) for tup in parsed))

    for i, parsed in zip(parts, parsed_parts):
        tokens[i] = tokens[i]._replace(src=unparse_parsed_string(parsed))


def _fix_encode_to_binary(tokens: List[Token], i: int) -> None:
    # .encode()
    if (
            i + 2 < len(tokens) and
            tokens[i + 1].src == '(' and
            tokens[i + 2].src == ')'
    ):
        victims = slice(i - 1, i + 3)
        latin1_ok = False
    # .encode('encoding')
    elif (
            i + 3 < len(tokens) and
            tokens[i + 1].src == '(' and
            tokens[i + 2].name == 'STRING' and
            tokens[i + 3].src == ')'
    ):
        victims = slice(i - 1, i + 4)
        prefix, rest = parse_string_literal(tokens[i + 2].src)
        if 'f' in prefix.lower():
            return
        encoding = ast.literal_eval(prefix + rest)
        if _is_codec(encoding, 'ascii') or _is_codec(encoding, 'utf-8'):
            latin1_ok = False
        elif _is_codec(encoding, 'iso8859-1'):
            latin1_ok = True
        else:
            return
    else:
        return

    parts = rfind_string_parts(tokens, i - 2)
    if not parts:
        return

    for part in parts:
        prefix, rest = parse_string_literal(tokens[part].src)
        escapes = set(ESCAPE_RE.findall(rest))
        if (
                not _is_ascii(rest) or
                '\\u' in escapes or
                '\\U' in escapes or
                '\\N' in escapes or
                ('\\x' in escapes and not latin1_ok) or
                'f' in prefix.lower()
        ):
            return

    for part in parts:
        prefix, rest = parse_string_literal(tokens[part].src)
        prefix = 'b' + prefix.replace('u', '').replace('U', '')
        tokens[part] = tokens[part]._replace(src=prefix + rest)
    del tokens[victims]


def _build_import_removals() -> Dict[MinVersion, Dict[str, Tuple[str, ...]]]:
    ret = {}
    future: Tuple[Tuple[MinVersion, Tuple[str, ...]], ...] = (
        ((2, 7), ('nested_scopes', 'generators', 'with_statement')),
        (
            (3,), (
                'absolute_import', 'division', 'print_function',
                'unicode_literals',
            ),
        ),
        ((3, 6), ()),
        ((3, 7), ('generator_stop',)),
        ((3, 8), ()),
    )

    prev: Tuple[str, ...] = ()
    for min_version, names in future:
        prev += names
        ret[min_version] = {'__future__': prev}
    # see reorder_python_imports
    for k, v in ret.items():
        if k >= (3,):
            v.update({
                'builtins': (
                    'ascii', 'bytes', 'chr', 'dict', 'filter', 'hex', 'input',
                    'int', 'list', 'map', 'max', 'min', 'next', 'object',
                    'oct', 'open', 'pow', 'range', 'round', 'str', 'super',
                    'zip', '*',
                ),
                'io': ('open',),
                'six': ('callable', 'next'),
                'six.moves': ('filter', 'input', 'map', 'range', 'zip'),
            })
    return ret


IMPORT_REMOVALS = _build_import_removals()


def _fix_import_removals(
        tokens: List[Token],
        start: int,
        min_version: MinVersion,
) -> None:
    i = start + 1
    name_parts = []
    while tokens[i].src != 'import':
        if tokens[i].name in {'NAME', 'OP'}:
            name_parts.append(tokens[i].src)
        i += 1

    modname = ''.join(name_parts)
    if modname not in IMPORT_REMOVALS[min_version]:
        return

    found: List[Optional[int]] = []
    i += 1
    while tokens[i].name not in {'NEWLINE', 'ENDMARKER'}:
        if tokens[i].name == 'NAME' or tokens[i].src == '*':
            # don't touch aliases
            if (
                    found and found[-1] is not None and
                    tokens[found[-1]].src == 'as'
            ):
                found[-2:] = [None]
            else:
                found.append(i)
        i += 1
    # depending on the version of python, some will not emit NEWLINE('') at the
    # end of a file which does not end with a newline (for example 3.6.5)
    if tokens[i].name == 'ENDMARKER':  # pragma: no cover
        i -= 1

    remove_names = IMPORT_REMOVALS[min_version][modname]
    to_remove = [
        x for x in found if x is not None and tokens[x].src in remove_names
    ]
    if len(to_remove) == len(found):
        del tokens[start:i + 1]
    else:
        for idx in reversed(to_remove):
            if found[0] == idx:  # look forward until next name and del
                j = idx + 1
                while tokens[j].name != 'NAME':
                    j += 1
                del tokens[idx:j]
            else:  # look backward for comma and del
                j = idx
                while tokens[j].src != ',':
                    j -= 1
                del tokens[j:idx + 1]


def _fix_tokens(contents_text: str, min_version: MinVersion) -> str:
    remove_u = (
        min_version >= (3,) or
        _imports_future(contents_text, 'unicode_literals')
    )

    try:
        tokens = src_to_tokens(contents_text)
    except tokenize.TokenError:
        return contents_text
    for i, token in reversed_enumerate(tokens):
        if token.name == 'NUMBER':
            tokens[i] = token._replace(src=_fix_long(_fix_octal(token.src)))
        elif token.name == 'STRING':
            tokens[i] = _fix_ur_literals(tokens[i])
            if remove_u:
                tokens[i] = _remove_u_prefix(tokens[i])
            tokens[i] = _fix_escape_sequences(tokens[i])
        elif token.src == '(':
            _fix_extraneous_parens(tokens, i)
        elif token.src == 'format' and i > 0 and tokens[i - 1].src == '.':
            _fix_format_literal(tokens, i - 2)
        elif token.src == 'encode' and i > 0 and tokens[i - 1].src == '.':
            _fix_encode_to_binary(tokens, i)
        elif (
                min_version >= (3,) and
                token.utf8_byte_offset == 0 and
                token.line < 3 and
                token.name == 'COMMENT' and
                tokenize.cookie_re.match(token.src)
        ):
            del tokens[i]
            assert tokens[i].name == 'NL', tokens[i].name
            del tokens[i]
        elif token.src == 'from' and token.utf8_byte_offset == 0:
            _fix_import_removals(tokens, i, min_version)
    return tokens_to_src(tokens).lstrip()


MAPPING_KEY_RE = re.compile(r'\(([^()]*)\)')
CONVERSION_FLAG_RE = re.compile('[#0+ -]*')
WIDTH_RE = re.compile(r'(?:\*|\d*)')
PRECISION_RE = re.compile(r'(?:\.(?:\*|\d*))?')
LENGTH_RE = re.compile('[hlL]?')


def _must_match(regex: Pattern[str], string: str, pos: int) -> Match[str]:
    match = regex.match(string, pos)
    assert match is not None
    return match


def parse_percent_format(s: str) -> Tuple[PercentFormat, ...]:
    def _parse_inner() -> Generator[PercentFormat, None, None]:
        string_start = 0
        string_end = 0
        in_fmt = False

        i = 0
        while i < len(s):
            if not in_fmt:
                try:
                    i = s.index('%', i)
                except ValueError:  # no more % fields!
                    yield s[string_start:], None
                    return
                else:
                    string_end = i
                    i += 1
                    in_fmt = True
            else:
                key_match = MAPPING_KEY_RE.match(s, i)
                if key_match:
                    key: Optional[str] = key_match.group(1)
                    i = key_match.end()
                else:
                    key = None

                conversion_flag_match = _must_match(CONVERSION_FLAG_RE, s, i)
                conversion_flag = conversion_flag_match.group() or None
                i = conversion_flag_match.end()

                width_match = _must_match(WIDTH_RE, s, i)
                width = width_match.group() or None
                i = width_match.end()

                precision_match = _must_match(PRECISION_RE, s, i)
                precision = precision_match.group() or None
                i = precision_match.end()

                # length modifier is ignored
                i = _must_match(LENGTH_RE, s, i).end()

                try:
                    conversion = s[i]
                except IndexError:
                    raise ValueError('end-of-string while parsing format')
                i += 1

                fmt = (key, conversion_flag, width, precision, conversion)
                yield s[string_start:string_end], fmt

                in_fmt = False
                string_start = i

        if in_fmt:
            raise ValueError('end-of-string while parsing format')

    return tuple(_parse_inner())


class FindPercentFormats(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: Dict[Offset, ast.BinOp] = {}

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Str):
            try:
                parsed = parse_percent_format(node.left.s)
            except ValueError:
                pass
            else:
                for _, fmt in parsed:
                    if not fmt:
                        continue
                    key, conversion_flag, width, precision, conversion = fmt
                    # timid: these require out-of-order parameter consumption
                    if width == '*' or precision == '.*':
                        break
                    # these conversions require modification of parameters
                    if conversion in {'d', 'i', 'u', 'c'}:
                        break
                    # timid: py2: %#o formats different from {:#o} (--py3?)
                    if '#' in (conversion_flag or '') and conversion == 'o':
                        break
                    # no equivalent in format
                    if key == '':
                        break
                    # timid: py2: conversion is subject to modifiers (--py3?)
                    nontrivial_fmt = any((conversion_flag, width, precision))
                    if conversion == '%' and nontrivial_fmt:
                        break
                    # no equivalent in format
                    if conversion in {'a', 'r'} and nontrivial_fmt:
                        break
                    # all dict substitutions must be named
                    if isinstance(node.right, ast.Dict) and not key:
                        break
                else:
                    self.found[_ast_to_offset(node)] = node
        self.generic_visit(node)


def _simplify_conversion_flag(flag: str) -> str:
    parts: List[str] = []
    for c in flag:
        if c in parts:
            continue
        c = c.replace('-', '<')
        parts.append(c)
        if c == '<' and '0' in parts:
            parts.remove('0')
        elif c == '+' and ' ' in parts:
            parts.remove(' ')
    return ''.join(parts)


NAMED_UNICODE_RE = re.compile(r'(?<!\\)(?:\\\\)*\\N{{([^}]+)}}')


def _percent_to_format(s: str) -> str:
    def _handle_part(part: PercentFormat) -> str:
        s, fmt = part
        s = s.replace('{', '{{').replace('}', '}}')
        if r'\N' in s:
            # Convert \N{{snowman}} back to \N{snowman}
            s = NAMED_UNICODE_RE.sub(r'\\N{\1}', s)
        if fmt is None:
            return s
        else:
            key, conversion_flag, width, precision, conversion = fmt
            if conversion == '%':
                return s + '%'
            parts = [s, '{']
            if width and conversion == 's' and not conversion_flag:
                conversion_flag = '>'
            if conversion == 's':
                conversion = ''
            if key:
                parts.append(key)
            if conversion in {'r', 'a'}:
                converter = f'!{conversion}'
                conversion = ''
            else:
                converter = ''
            if any((conversion_flag, width, precision, conversion)):
                parts.append(':')
            if conversion_flag:
                parts.append(_simplify_conversion_flag(conversion_flag))
            parts.extend(x for x in (width, precision, conversion) if x)
            parts.extend(converter)
            parts.append('}')
            return ''.join(parts)

    return ''.join(_handle_part(part) for part in parse_percent_format(s))


def _is_ascii(s: str) -> bool:
    if sys.version_info >= (3, 7):  # pragma: no cover (py37+)
        return s.isascii()
    else:  # pragma: no cover (<py37)
        return all(c in string.printable for c in s)


def _fix_percent_format_tuple(
        tokens: List[Token],
        start: int,
        node: ast.BinOp,
) -> None:
    # TODO: this is overly timid
    paren = start + 4
    if tokens_to_src(tokens[start + 1:paren + 1]) != ' % (':
        return

    victims = _victims(tokens, paren, node.right, gen=False)
    victims.ends.pop()

    for index in reversed(victims.starts + victims.ends):
        _remove_brace(tokens, index)

    newsrc = _percent_to_format(tokens[start].src)
    tokens[start] = tokens[start]._replace(src=newsrc)
    tokens[start + 1:paren] = [Token('Format', '.format'), Token('OP', '(')]


def _fix_percent_format_dict(
        tokens: List[Token],
        start: int,
        node: ast.BinOp,
) -> None:
    seen_keys: Set[str] = set()
    keys = {}

    # the caller has enforced this
    assert isinstance(node.right, ast.Dict)
    for k in node.right.keys:
        # not a string key
        if not isinstance(k, ast.Str):
            return
        # duplicate key
        elif k.s in seen_keys:
            return
        # not an identifier
        elif not k.s.isidentifier():
            return
        # a keyword
        elif k.s in _KEYWORDS:
            return
        seen_keys.add(k.s)
        keys[_ast_to_offset(k)] = k

    # TODO: this is overly timid
    brace = start + 4
    if tokens_to_src(tokens[start + 1:brace + 1]) != ' % {':
        return

    victims = _victims(tokens, brace, node.right, gen=False)
    brace_end = victims.ends[-1]

    key_indices = []
    for i, token in enumerate(tokens[brace:brace_end], brace):
        key = keys.pop(token.offset, None)
        if key is None:
            continue
        # we found the key, but the string didn't match (implicit join?)
        elif ast.literal_eval(token.src) != key.s:
            return
        # the map uses some strange syntax that's not `'key': value`
        elif tokens_to_src(tokens[i + 1:i + 3]) != ': ':
            return
        else:
            key_indices.append((i, key.s))
    assert not keys, keys

    tokens[brace_end] = tokens[brace_end]._replace(src=')')
    for (key_index, s) in reversed(key_indices):
        tokens[key_index:key_index + 3] = [Token('CODE', f'{s}=')]
    newsrc = _percent_to_format(tokens[start].src)
    tokens[start] = tokens[start]._replace(src=newsrc)
    tokens[start + 1:brace + 1] = [Token('CODE', '.format'), Token('OP', '(')]


def _fix_percent_format(contents_text: str) -> str:
    try:
        ast_obj = ast_parse(contents_text)
    except SyntaxError:
        return contents_text

    visitor = FindPercentFormats()
    visitor.visit(ast_obj)

    if not visitor.found:
        return contents_text

    try:
        tokens = src_to_tokens(contents_text)
    except tokenize.TokenError:  # pragma: no cover (bpo-2180)
        return contents_text

    for i, token in reversed_enumerate(tokens):
        node = visitor.found.get(token.offset)
        if node is None:
            continue

        if isinstance(node.right, ast.Tuple):
            _fix_percent_format_tuple(tokens, i, node)
        elif isinstance(node.right, ast.Dict):
            _fix_percent_format_dict(tokens, i, node)

    return tokens_to_src(tokens)


SIX_SIMPLE_ATTRS = {
    'text_type': 'str',
    'binary_type': 'bytes',
    'class_types': '(type,)',
    'string_types': '(str,)',
    'integer_types': '(int,)',
    'unichr': 'chr',
    'iterbytes': 'iter',
    'print_': 'print',
    'exec_': 'exec',
    'advance_iterator': 'next',
    'next': 'next',
    'callable': 'callable',
}
SIX_TYPE_CTX_ATTRS = {
    'class_types': 'type',
    'string_types': 'str',
    'integer_types': 'int',
}
SIX_CALLS = {
    'u': '{args[0]}',
    'byte2int': '{args[0]}[0]',
    'indexbytes': '{args[0]}[{rest}]',
    'iteritems': '{args[0]}.items()',
    'iterkeys': '{args[0]}.keys()',
    'itervalues': '{args[0]}.values()',
    'viewitems': '{args[0]}.items()',
    'viewkeys': '{args[0]}.keys()',
    'viewvalues': '{args[0]}.values()',
    'create_unbound_method': '{args[0]}',
    'get_unbound_function': '{args[0]}',
    'get_method_function': '{args[0]}.__func__',
    'get_method_self': '{args[0]}.__self__',
    'get_function_closure': '{args[0]}.__closure__',
    'get_function_code': '{args[0]}.__code__',
    'get_function_defaults': '{args[0]}.__defaults__',
    'get_function_globals': '{args[0]}.__globals__',
    'assertCountEqual': '{args[0]}.assertCountEqual({rest})',
    'assertRaisesRegex': '{args[0]}.assertRaisesRegex({rest})',
    'assertRegex': '{args[0]}.assertRegex({rest})',
}
SIX_INT2BYTE_TMPL = 'bytes(({args[0]},))'
SIX_B_TMPL = 'b{args[0]}'
WITH_METACLASS_NO_BASES_TMPL = 'metaclass={args[0]}'
WITH_METACLASS_BASES_TMPL = '{rest}, metaclass={args[0]}'
RAISE_FROM_TMPL = 'raise {args[0]} from {rest}'
RERAISE_TMPL = 'raise'
RERAISE_2_TMPL = 'raise {args[1]}.with_traceback(None)'
RERAISE_3_TMPL = 'raise {args[1]}.with_traceback({args[2]})'
SIX_NATIVE_STR = frozenset(('ensure_str', 'ensure_text', 'text_type'))
U_MODE_REMOVE = frozenset(('U', 'Ur', 'rU', 'r', 'rt', 'tr'))
U_MODE_REPLACE_R = frozenset(('Ub', 'bU'))
U_MODE_REMOVE_U = frozenset(('rUb', 'Urb', 'rbU', 'Ubr', 'bUr', 'brU'))
U_MODE_REPLACE = U_MODE_REPLACE_R | U_MODE_REMOVE_U

PEP585_BUILTINS = {
    k: k.lower()
    for k in ('Dict', 'FrozenSet', 'List', 'Set', 'Tuple', 'Type')
}


def _all_isinstance(
        vals: Iterable[Any],
        tp: Union[Type[Any], Tuple[Type[Any], ...]],
) -> bool:
    return all(isinstance(v, tp) for v in vals)


def fields_same(n1: ast.AST, n2: ast.AST) -> bool:
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


def targets_same(target: ast.AST, yield_value: ast.AST) -> bool:
    for t1, t2 in zip(ast.walk(target), ast.walk(yield_value)):
        # ignore `ast.Load` / `ast.Store`
        if _all_isinstance((t1, t2), ast.expr_context):
            continue
        elif type(t1) != type(t2):
            return False
        elif not fields_same(t1, t2):
            return False
    else:
        return True


def _is_codec(encoding: str, name: str) -> bool:
    try:
        return codecs.lookup(encoding).name == name
    except LookupError:
        return False


class FindPy3Plus(ast.NodeVisitor):
    OS_ERROR_ALIASES = frozenset((
        'EnvironmentError',
        'IOError',
        'WindowsError',
    ))

    OS_ERROR_ALIAS_MODULES = frozenset((
        'mmap',
        'select',
        'socket',
    ))

    FROM_IMPORTED_MODULES = OS_ERROR_ALIAS_MODULES.union((
        'functools', 'six', 'typing',
    ))

    MOCK_MODULES = frozenset(('mock', 'mock.mock'))

    class ClassInfo:
        def __init__(self, name: str) -> None:
            self.name = name
            self.def_depth = 0
            self.first_arg_name = ''

    class Scope:
        def __init__(self) -> None:
            self.reads: Set[str] = set()
            self.writes: Set[str] = set()

            self.yield_from_fors: Set[Offset] = set()
            self.yield_from_names: Dict[str, Set[Offset]]
            self.yield_from_names = collections.defaultdict(set)

    def __init__(self, version: Tuple[int, ...], keep_mock: bool) -> None:
        self._version = version
        self._find_mock = not keep_mock

        self.bases_to_remove: Set[Offset] = set()

        self.encode_calls: Dict[Offset, ast.Call] = {}

        self._exc_info_imported = False
        self._version_info_imported = False
        self.if_py3_blocks: Set[Offset] = set()
        self.if_py2_blocks_else: Set[Offset] = set()
        self.if_py3_blocks_else: Set[Offset] = set()
        self.metaclass_type_assignments: Set[Offset] = set()

        self.native_literals: Set[Offset] = set()

        self._from_imports: Dict[str, Set[str]] = collections.defaultdict(set)
        self.io_open_calls: Set[Offset] = set()
        self.mock_mock: Set[Offset] = set()
        self.mock_absolute_imports: Set[Offset] = set()
        self.mock_relative_imports: Set[Offset] = set()
        self.open_mode_calls: Set[Offset] = set()
        self.os_error_alias_calls: Set[Offset] = set()
        self.os_error_alias_simple: Dict[Offset, NameOrAttr] = {}
        self.os_error_alias_excepts: Set[Offset] = set()

        self.six_add_metaclass: Set[Offset] = set()
        self.six_b: Set[Offset] = set()
        self.six_calls: Dict[Offset, ast.Call] = {}
        self.six_calls_int2byte: Set[Offset] = set()
        self.six_iter: Dict[Offset, ast.Call] = {}
        self._previous_node: Optional[ast.AST] = None
        self.six_raise_from: Set[Offset] = set()
        self.six_reraise: Set[Offset] = set()
        self.six_remove_decorators: Set[Offset] = set()
        self.six_simple: Dict[Offset, NameOrAttr] = {}
        self.six_type_ctx: Dict[Offset, NameOrAttr] = {}
        self.six_with_metaclass: Set[Offset] = set()

        self._in_type_annotation = False
        self.typing_builtin_renames: Dict[Offset, NameOrAttr] = {}

        self._class_info_stack: List[FindPy3Plus.ClassInfo] = []
        self._in_comp = 0
        self.super_calls: Dict[Offset, ast.Call] = {}
        self._in_async_def = False
        self._scope_stack: List[FindPy3Plus.Scope] = []
        self.yield_from_fors: Set[Offset] = set()

        self.no_arg_decorators: Set[Offset] = set()

    def _is_six(self, node: ast.expr, names: Container[str]) -> bool:
        return (
            isinstance(node, ast.Name) and
            node.id in names and
            node.id in self._from_imports['six']
        ) or (
            isinstance(node, ast.Attribute) and
            isinstance(node.value, ast.Name) and
            node.value.id == 'six' and
            node.attr in names
        )

    def _is_star_sys_exc_info(self, node: ast.Call) -> bool:
        return (
            len(node.args) == 1 and
            isinstance(node.args[0], ast.Starred) and
            isinstance(node.args[0].value, ast.Call) and
            self._is_exc_info(node.args[0].value.func)
        )

    def _is_lru_cache(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Name) and
            node.id == 'lru_cache' and
            node.id in self._from_imports['functools']
        ) or (
            isinstance(node, ast.Attribute) and
            isinstance(node.value, ast.Name) and
            node.value.id == 'functools' and
            node.attr == 'lru_cache'
        )

    def _is_mock_mock(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute) and
            isinstance(node.value, ast.Name) and
            node.value.id == 'mock' and
            node.attr == 'mock'
        )

    def _is_io_open(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute) and
            isinstance(node.value, ast.Name) and
            node.value.id == 'io' and
            node.attr == 'open'
        )

    def _is_os_error_alias(self, node: Optional[ast.expr]) -> bool:
        return (
            isinstance(node, ast.Name) and
            node.id in self.OS_ERROR_ALIASES
        ) or (
            isinstance(node, ast.Name) and
            node.id == 'error' and
            (
                node.id in self._from_imports['mmap'] or
                node.id in self._from_imports['select'] or
                node.id in self._from_imports['socket']
            )
        ) or (
            isinstance(node, ast.Attribute) and
            isinstance(node.value, ast.Name) and
            node.value.id in self.OS_ERROR_ALIAS_MODULES and
            node.attr == 'error'
        )

    def _is_exc_info(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Name) and
            node.id == 'exc_info' and
            self._exc_info_imported
        ) or (
            isinstance(node, ast.Attribute) and
            isinstance(node.value, ast.Name) and
            node.value.id == 'sys' and
            node.attr == 'exc_info'
        )

    def _is_version_info(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Name) and
            node.id == 'version_info' and
            self._version_info_imported
        ) or (
            isinstance(node, ast.Attribute) and
            isinstance(node.value, ast.Name) and
            node.value.id == 'sys' and
            node.attr == 'version_info'
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.level:
            if node.module in self.FROM_IMPORTED_MODULES:
                for name in node.names:
                    if not name.asname:
                        self._from_imports[node.module].add(name.name)
            elif self._find_mock and node.module in self.MOCK_MODULES:
                self.mock_relative_imports.add(_ast_to_offset(node))
            elif node.module == 'sys' and any(
                name.name == 'exc_info' and not name.asname
                for name in node.names
            ):
                self._exc_info_imported = True
            elif node.module == 'sys' and any(
                name.name == 'version_info' and not name.asname
                for name in node.names
            ):
                self._version_info_imported = True
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if (
                self._find_mock and
                len(node.names) == 1 and
                node.names[0].name in self.MOCK_MODULES
        ):
            self.mock_absolute_imports.add(_ast_to_offset(node))

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            if self._is_six(decorator, ('python_2_unicode_compatible',)):
                self.six_remove_decorators.add(_ast_to_offset(decorator))
            elif (
                    isinstance(decorator, ast.Call) and
                    self._is_six(decorator.func, ('add_metaclass',)) and
                    not _starargs(decorator)
            ):
                self.six_add_metaclass.add(_ast_to_offset(decorator))

        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == 'object':
                self.bases_to_remove.add(_ast_to_offset(base))
            elif self._is_six(base, ('Iterator',)):
                self.bases_to_remove.add(_ast_to_offset(base))

        if (
                len(node.bases) == 1 and
                isinstance(node.bases[0], ast.Call) and
                self._is_six(node.bases[0].func, ('with_metaclass',)) and
                not _starargs(node.bases[0])
        ):
            self.six_with_metaclass.add(_ast_to_offset(node.bases[0]))

        self._class_info_stack.append(FindPy3Plus.ClassInfo(node.name))
        self.generic_visit(node)
        self._class_info_stack.pop()

    @contextlib.contextmanager
    def _track_def_depth(
            self,
            node: AnyFunctionDef,
    ) -> Generator[None, None, None]:
        class_info = self._class_info_stack[-1]
        class_info.def_depth += 1
        if class_info.def_depth == 1 and node.args.args:
            class_info.first_arg_name = node.args.args[0].arg
        try:
            yield
        finally:
            class_info.def_depth -= 1

    @contextlib.contextmanager
    def _scope(self) -> Generator[None, None, None]:
        self._scope_stack.append(FindPy3Plus.Scope())
        try:
            yield
        finally:
            info = self._scope_stack.pop()
            # discard any that were referenced outside of the loop
            for name in info.reads:
                offsets = info.yield_from_names[name]
                info.yield_from_fors.difference_update(offsets)
            self.yield_from_fors.update(info.yield_from_fors)
            if self._scope_stack:
                cell_reads = info.reads - info.writes
                self._scope_stack[-1].reads.update(cell_reads)

    def _visit_annotation(self, node: ast.AST) -> None:
        orig, self._in_type_annotation = self._in_type_annotation, True
        self.generic_visit(node)
        self._in_type_annotation = orig

    def _visit_func(self, node: AnyFunctionDef) -> None:
        with contextlib.ExitStack() as ctx, self._scope():
            if self._class_info_stack:
                ctx.enter_context(self._track_def_depth(node))

            if not isinstance(node, ast.Lambda) and node.returns is not None:
                self._visit_annotation(node.returns)

            self.generic_visit(node)

    def _visit_sync_func(self, node: SyncFunctionDef) -> None:
        self._in_async_def, orig = False, self._in_async_def
        self._visit_func(node)
        self._in_async_def = orig

    visit_FunctionDef = visit_Lambda = _visit_sync_func

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._in_async_def, orig = True, self._in_async_def
        self._visit_func(node)
        self._in_async_def = orig

    def visit_arg(self, node: ast.arg) -> None:
        if node.annotation is not None:
            self._visit_annotation(node.annotation)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_annotation(node.annotation)
        self.generic_visit(node)

    def _visit_comp(self, node: ast.expr) -> None:
        self._in_comp += 1
        with self._scope():
            self.generic_visit(node)
        self._in_comp -= 1

    visit_ListComp = visit_SetComp = _visit_comp
    visit_DictComp = visit_GeneratorExp = _visit_comp

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._is_six(node, SIX_SIMPLE_ATTRS):
            self.six_simple[_ast_to_offset(node)] = node
        elif self._find_mock and self._is_mock_mock(node):
            self.mock_mock.add(_ast_to_offset(node))
        elif (
                (
                    self._version >= (3, 9) or
                    self._in_type_annotation
                ) and
                isinstance(node.value, ast.Name) and
                node.value.id == 'typing' and
                node.attr in PEP585_BUILTINS
        ):
            self.typing_builtin_renames[_ast_to_offset(node)] = node
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if self._is_six(node, SIX_SIMPLE_ATTRS):
            self.six_simple[_ast_to_offset(node)] = node
        elif (
                (
                    self._version >= (3, 9) or
                    self._in_type_annotation
                ) and
                node.id in PEP585_BUILTINS and
                node.id in self._from_imports['typing']
        ):
            self.typing_builtin_renames[_ast_to_offset(node)] = node

        if self._scope_stack:
            if isinstance(node.ctx, ast.Load):
                self._scope_stack[-1].reads.add(node.id)
            elif isinstance(node.ctx, (ast.Store, ast.Del)):
                self._scope_stack[-1].writes.add(node.id)
            else:
                raise AssertionError(node)

        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            htype = handler.type
            if self._is_os_error_alias(htype):
                assert isinstance(htype, (ast.Name, ast.Attribute))
                self.os_error_alias_simple[_ast_to_offset(htype)] = htype
            elif (
                    isinstance(htype, ast.Tuple) and
                    any(
                        self._is_os_error_alias(elt)
                        for elt in htype.elts
                    )
            ):
                self.os_error_alias_excepts.add(_ast_to_offset(htype))

        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        exc = node.exc

        if exc is not None and self._is_os_error_alias(exc):
            assert isinstance(exc, (ast.Name, ast.Attribute))
            self.os_error_alias_simple[_ast_to_offset(exc)] = exc
        elif (
                isinstance(exc, ast.Call) and
                self._is_os_error_alias(exc.func)
        ):
            self.os_error_alias_calls.add(_ast_to_offset(exc))

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
                isinstance(node.func, ast.Name) and
                node.func.id in {'isinstance', 'issubclass'} and
                len(node.args) == 2 and
                self._is_six(node.args[1], SIX_TYPE_CTX_ATTRS)
        ):
            arg = node.args[1]
            # _is_six() enforces this
            assert isinstance(arg, (ast.Name, ast.Attribute))
            self.six_type_ctx[_ast_to_offset(node.args[1])] = arg
        elif self._is_six(node.func, ('b', 'ensure_binary')):
            self.six_b.add(_ast_to_offset(node))
        elif (
                self._is_six(node.func, SIX_CALLS) and
                node.args and
                not _starargs(node)
        ):
            self.six_calls[_ast_to_offset(node)] = node
        elif (
                self._is_six(node.func, ('int2byte',)) and
                node.args and
                not _starargs(node)
        ):
            self.six_calls_int2byte.add(_ast_to_offset(node))
        elif (
                isinstance(node.func, ast.Name) and
                node.func.id == 'next' and
                not _starargs(node) and
                len(node.args) == 1 and
                isinstance(node.args[0], ast.Call) and
                self._is_six(
                    node.args[0].func,
                    ('iteritems', 'iterkeys', 'itervalues'),
                ) and
                not _starargs(node.args[0])
        ):
            self.six_iter[_ast_to_offset(node.args[0])] = node.args[0]
        elif (
                isinstance(self._previous_node, ast.Expr) and
                self._is_six(node.func, ('raise_from',)) and
                not _starargs(node)
        ):
            self.six_raise_from.add(_ast_to_offset(node))
        elif (
                isinstance(self._previous_node, ast.Expr) and
                self._is_six(node.func, ('reraise',)) and
                (not _starargs(node) or self._is_star_sys_exc_info(node))
        ):
            self.six_reraise.add(_ast_to_offset(node))
        elif (
                not self._in_comp and
                self._class_info_stack and
                self._class_info_stack[-1].def_depth == 1 and
                isinstance(node.func, ast.Name) and
                node.func.id == 'super' and
                len(node.args) == 2 and
                isinstance(node.args[0], ast.Name) and
                isinstance(node.args[1], ast.Name) and
                node.args[0].id == self._class_info_stack[-1].name and
                node.args[1].id == self._class_info_stack[-1].first_arg_name
        ):
            self.super_calls[_ast_to_offset(node)] = node
        elif (
                (
                    self._is_six(node.func, SIX_NATIVE_STR) or
                    isinstance(node.func, ast.Name) and node.func.id == 'str'
                ) and
                not node.keywords and
                not _starargs(node) and
                (
                    len(node.args) == 0 or
                    (
                        len(node.args) == 1 and
                        isinstance(node.args[0], ast.Str)
                    )
                )
        ):
            self.native_literals.add(_ast_to_offset(node))
        elif (
                isinstance(node.func, ast.Attribute) and
                isinstance(node.func.value, ast.Str) and
                node.func.attr == 'encode' and
                not _starargs(node) and
                len(node.args) == 1 and
                isinstance(node.args[0], ast.Str) and
                _is_codec(node.args[0].s, 'utf-8')
        ):
            self.encode_calls[_ast_to_offset(node)] = node
        elif self._is_io_open(node.func):
            self.io_open_calls.add(_ast_to_offset(node))
        elif (
                isinstance(node.func, ast.Name) and
                node.func.id == 'open' and
                not _starargs(node) and
                len(node.args) >= 2 and
                isinstance(node.args[1], ast.Str) and (
                    node.args[1].s in U_MODE_REPLACE or
                    (len(node.args) == 2 and node.args[1].s in U_MODE_REMOVE)
                )
        ):
            self.open_mode_calls.add(_ast_to_offset(node))
        elif (
                not node.args and
                not node.keywords and
                self._is_lru_cache(node.func)
        ):
            self.no_arg_decorators.add(_ast_to_offset(node))

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if (
                len(node.targets) == 1 and
                isinstance(node.targets[0], ast.Name) and
                node.targets[0].col_offset == 0 and
                node.targets[0].id == '__metaclass__' and
                isinstance(node.value, ast.Name) and
                node.value.id == 'type'
        ):
            self.metaclass_type_assignments.add(_ast_to_offset(node))

        self.generic_visit(node)

    @staticmethod
    def _eq(test: ast.Compare, n: int) -> bool:
        return (
            isinstance(test.ops[0], ast.Eq) and
            isinstance(test.comparators[0], ast.Num) and
            test.comparators[0].n == n
        )

    @staticmethod
    def _compare_to_3(
        test: ast.Compare,
        op: Union[Type[ast.cmpop], Tuple[Type[ast.cmpop], ...]],
    ) -> bool:
        if not (
                isinstance(test.ops[0], op) and
                isinstance(test.comparators[0], ast.Tuple) and
                len(test.comparators[0].elts) >= 1 and
                all(isinstance(n, ast.Num) for n in test.comparators[0].elts)
        ):
            return False

        # checked above but mypy needs help
        elts = cast('List[ast.Num]', test.comparators[0].elts)

        return elts[0].n == 3 and all(n.n == 0 for n in elts[1:])

    def visit_If(self, node: ast.If) -> None:
        if (
                # if six.PY2:
                self._is_six(node.test, ('PY2',)) or
                # if not six.PY3:
                (
                    isinstance(node.test, ast.UnaryOp) and
                    isinstance(node.test.op, ast.Not) and
                    self._is_six(node.test.operand, ('PY3',))
                ) or
                # sys.version_info == 2 or < (3,)
                (
                    isinstance(node.test, ast.Compare) and
                    self._is_version_info(node.test.left) and
                    len(node.test.ops) == 1 and (
                        self._eq(node.test, 2) or
                        self._compare_to_3(node.test, ast.Lt)
                    )
                )
        ):
            if node.orelse and not isinstance(node.orelse[0], ast.If):
                self.if_py2_blocks_else.add(_ast_to_offset(node))
        elif (
                # if six.PY3:
                self._is_six(node.test, 'PY3') or
                # if not six.PY2:
                (
                    isinstance(node.test, ast.UnaryOp) and
                    isinstance(node.test.op, ast.Not) and
                    self._is_six(node.test.operand, ('PY2',))
                ) or
                # sys.version_info == 3 or >= (3,) or > (3,)
                (
                    isinstance(node.test, ast.Compare) and
                    self._is_version_info(node.test.left) and
                    len(node.test.ops) == 1 and (
                        self._eq(node.test, 3) or
                        self._compare_to_3(node.test, (ast.Gt, ast.GtE))
                    )
                )
        ):
            if node.orelse and not isinstance(node.orelse[0], ast.If):
                self.if_py3_blocks_else.add(_ast_to_offset(node))
            elif not node.orelse:
                self.if_py3_blocks.add(_ast_to_offset(node))
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        if (
            not self._in_async_def and
            len(node.body) == 1 and
            isinstance(node.body[0], ast.Expr) and
            isinstance(node.body[0].value, ast.Yield) and
            node.body[0].value.value is not None and
            targets_same(node.target, node.body[0].value.value) and
            not node.orelse
        ):
            offset = _ast_to_offset(node)
            func_info = self._scope_stack[-1]
            func_info.yield_from_fors.add(offset)
            for target_node in ast.walk(node.target):
                if (
                        isinstance(target_node, ast.Name) and
                        isinstance(target_node.ctx, ast.Store)
                ):
                    func_info.yield_from_names[target_node.id].add(offset)
            # manually visit, but with target+body as a separate scope
            self.visit(node.iter)
            with self._scope():
                self.visit(node.target)
                for stmt in node.body:
                    self.visit(stmt)
                assert not node.orelse
        else:
            self.generic_visit(node)

    def generic_visit(self, node: ast.AST) -> None:
        self._previous_node = node
        super().generic_visit(node)


def _fixup_dedent_tokens(tokens: List[Token]) -> None:
    """For whatever reason the DEDENT / UNIMPORTANT_WS tokens are misordered

    | if True:
    |     if True:
    |         pass
    |     else:
    |^    ^- DEDENT
    |+----UNIMPORTANT_WS
    """
    for i, token in enumerate(tokens):
        if token.name == UNIMPORTANT_WS and tokens[i + 1].name == 'DEDENT':
            tokens[i], tokens[i + 1] = tokens[i + 1], tokens[i]


def _find_block_start(tokens: List[Token], i: int) -> int:
    depth = 0
    while depth or tokens[i].src != ':':
        if tokens[i].src in OPENING:
            depth += 1
        elif tokens[i].src in CLOSING:
            depth -= 1
        i += 1
    return i


class Block(NamedTuple):
    start: int
    colon: int
    block: int
    end: int
    line: bool

    def _initial_indent(self, tokens: List[Token]) -> int:
        if tokens[self.start].src.isspace():
            return len(tokens[self.start].src)
        else:
            return 0

    def _minimum_indent(self, tokens: List[Token]) -> int:
        block_indent = None
        for i in range(self.block, self.end):
            if (
                    tokens[i - 1].name in ('NL', 'NEWLINE') and
                    tokens[i].name in ('INDENT', UNIMPORTANT_WS)
            ):
                token_indent = len(tokens[i].src)
                if block_indent is None:
                    block_indent = token_indent
                else:
                    block_indent = min(block_indent, token_indent)

        assert block_indent is not None
        return block_indent

    def dedent(self, tokens: List[Token]) -> None:
        if self.line:
            return
        diff = self._minimum_indent(tokens) - self._initial_indent(tokens)
        for i in range(self.block, self.end):
            if (
                    tokens[i - 1].name in ('DEDENT', 'NL', 'NEWLINE') and
                    tokens[i].name in ('INDENT', UNIMPORTANT_WS)
            ):
                tokens[i] = tokens[i]._replace(src=tokens[i].src[diff:])

    def replace_condition(self, tokens: List[Token], new: List[Token]) -> None:
        tokens[self.start:self.colon] = new

    def _trim_end(self, tokens: List[Token]) -> 'Block':
        """the tokenizer reports the end of the block at the beginning of
        the next block
        """
        i = last_token = self.end - 1
        while tokens[i].name in NON_CODING_TOKENS | {'DEDENT', 'NEWLINE'}:
            # if we find an indented comment inside our block, keep it
            if (
                    tokens[i].name in {'NL', 'NEWLINE'} and
                    tokens[i + 1].name == UNIMPORTANT_WS and
                    len(tokens[i + 1].src) > self._initial_indent(tokens)
            ):
                break
            # otherwise we've found another line to remove
            elif tokens[i].name in {'NL', 'NEWLINE'}:
                last_token = i
            i -= 1
        return self._replace(end=last_token + 1)

    @classmethod
    def find(
            cls,
            tokens: List[Token],
            i: int,
            trim_end: bool = False,
    ) -> 'Block':
        if i > 0 and tokens[i - 1].name in {'INDENT', UNIMPORTANT_WS}:
            i -= 1
        start = i
        colon = _find_block_start(tokens, i)

        j = colon + 1
        while (
                tokens[j].name != 'NEWLINE' and
                tokens[j].name in NON_CODING_TOKENS
        ):
            j += 1

        if tokens[j].name == 'NEWLINE':  # multi line block
            block = j + 1
            while tokens[j].name != 'INDENT':
                j += 1
            level = 1
            j += 1
            while level:
                level += {'INDENT': 1, 'DEDENT': -1}.get(tokens[j].name, 0)
                j += 1
            ret = cls(start, colon, block, j, line=False)
            if trim_end:
                return ret._trim_end(tokens)
            else:
                return ret
        else:  # single line block
            block = j
            j = _find_end(tokens, j)
            return cls(start, colon, block, j, line=True)


def _find_end(tokens: List[Token], i: int) -> int:
    while tokens[i].name not in {'NEWLINE', 'ENDMARKER'}:
        i += 1

    # depending on the version of python, some will not emit
    # NEWLINE('') at the end of a file which does not end with a
    # newline (for example 3.6.5)
    if tokens[i].name == 'ENDMARKER':  # pragma: no cover
        i -= 1
    else:
        i += 1

    return i


def _find_if_else_block(tokens: List[Token], i: int) -> Tuple[Block, Block]:
    if_block = Block.find(tokens, i)
    i = if_block.end
    while tokens[i].src != 'else':
        i += 1
    else_block = Block.find(tokens, i, trim_end=True)
    return if_block, else_block


def _find_elif(tokens: List[Token], i: int) -> int:
    while tokens[i].src != 'elif':  # pragma: no cover (only for <3.8.1)
        i -= 1
    return i


def _remove_decorator(tokens: List[Token], i: int) -> None:
    while tokens[i - 1].src != '@':
        i -= 1
    if i > 1 and tokens[i - 2].name not in {'NEWLINE', 'NL'}:
        i -= 1
    end = i + 1
    while tokens[end].name != 'NEWLINE':
        end += 1
    del tokens[i - 1:end + 1]


def _remove_base_class(tokens: List[Token], i: int) -> None:
    # look forward and backward to find commas / parens
    brace_stack = []
    j = i
    while tokens[j].src not in {',', ':'}:
        if tokens[j].src == ')':
            brace_stack.append(j)
        j += 1
    right = j

    if tokens[right].src == ':':
        brace_stack.pop()
    else:
        # if there's a close-paren after a trailing comma
        j = right + 1
        while tokens[j].name in NON_CODING_TOKENS:
            j += 1
        if tokens[j].src == ')':
            while tokens[j].src != ':':
                j += 1
            right = j

    if brace_stack:
        last_part = brace_stack[-1]
    else:
        last_part = i

    j = i
    while brace_stack:
        if tokens[j].src == '(':
            brace_stack.pop()
        j -= 1

    while tokens[j].src not in {',', '('}:
        j -= 1
    left = j

    # single base, remove the entire bases
    if tokens[left].src == '(' and tokens[right].src == ':':
        del tokens[left:right]
    # multiple bases, base is first
    elif tokens[left].src == '(' and tokens[right].src != ':':
        # if there's space / comment afterwards remove that too
        while tokens[right + 1].name in {UNIMPORTANT_WS, 'COMMENT'}:
            right += 1
        del tokens[left + 1:right + 1]
    # multiple bases, base is not first
    else:
        del tokens[left:last_part + 1]


def _parse_call_args(
        tokens: List[Token],
        i: int,
) -> Tuple[List[Tuple[int, int]], int]:
    args = []
    stack = [i]
    i += 1
    arg_start = i

    while stack:
        token = tokens[i]

        if len(stack) == 1 and token.src == ',':
            args.append((arg_start, i))
            arg_start = i + 1
        elif token.src in BRACES:
            stack.append(i)
        elif token.src == BRACES[tokens[stack[-1]].src]:
            stack.pop()
            # if we're at the end, append that argument
            if not stack and tokens_to_src(tokens[arg_start:i]).strip():
                args.append((arg_start, i))

        i += 1

    return args, i


def _get_tmpl(mapping: Dict[str, str], node: NameOrAttr) -> str:
    if isinstance(node, ast.Name):
        return mapping[node.id]
    else:
        return mapping[node.attr]


def _arg_str(tokens: List[Token], start: int, end: int) -> str:
    return tokens_to_src(tokens[start:end]).strip()


def _replace_call(
        tokens: List[Token],
        start: int,
        end: int,
        args: List[Tuple[int, int]],
        tmpl: str,
        *,
        parens: Sequence[int] = (),
) -> None:
    arg_strs = [_arg_str(tokens, *arg) for arg in args]
    for paren in parens:
        arg_strs[paren] = f'({arg_strs[paren]})'

    start_rest = args[0][1] + 1
    while (
            start_rest < end and
            tokens[start_rest].name in {'COMMENT', UNIMPORTANT_WS}
    ):
        start_rest += 1

    rest = tokens_to_src(tokens[start_rest:end - 1])
    src = tmpl.format(args=arg_strs, rest=rest)
    tokens[start:end] = [Token('CODE', src)]


def _replace_yield(tokens: List[Token], i: int) -> None:
    in_token = _find_token(tokens, i, 'in')
    colon = _find_block_start(tokens, i)
    block = Block.find(tokens, i, trim_end=True)
    container = tokens_to_src(tokens[in_token + 1:colon]).strip()
    tokens[i:block.end] = [Token('CODE', f'yield from {container}\n')]


def _fix_py3_plus(
        contents_text: str,
        min_version: MinVersion,
        keep_mock: bool = False,
) -> str:
    try:
        ast_obj = ast_parse(contents_text)
    except SyntaxError:
        return contents_text

    pep585_rewrite = (
        min_version >= (3, 9) or
        _imports_future(contents_text, 'annotations')
    )

    visitor = FindPy3Plus(min_version, keep_mock)
    visitor.visit(ast_obj)

    if not any((
            visitor.bases_to_remove,
            visitor.encode_calls,
            visitor.if_py2_blocks_else,
            visitor.if_py3_blocks,
            visitor.if_py3_blocks_else,
            visitor.metaclass_type_assignments,
            visitor.native_literals,
            visitor.io_open_calls,
            visitor.open_mode_calls,
            visitor.mock_mock,
            visitor.mock_absolute_imports,
            visitor.mock_relative_imports,
            visitor.os_error_alias_calls,
            visitor.os_error_alias_simple,
            visitor.os_error_alias_excepts,
            visitor.no_arg_decorators,
            visitor.six_add_metaclass,
            visitor.six_b,
            visitor.six_calls,
            visitor.six_calls_int2byte,
            visitor.six_iter,
            visitor.six_raise_from,
            visitor.six_reraise,
            visitor.six_remove_decorators,
            visitor.six_simple,
            visitor.six_type_ctx,
            visitor.six_with_metaclass,
            visitor.super_calls,
            visitor.typing_builtin_renames,
            visitor.yield_from_fors,
    )):
        return contents_text

    try:
        tokens = src_to_tokens(contents_text)
    except tokenize.TokenError:  # pragma: no cover (bpo-2180)
        return contents_text

    _fixup_dedent_tokens(tokens)

    def _replace(i: int, mapping: Dict[str, str], node: NameOrAttr) -> None:
        new_token = Token('CODE', _get_tmpl(mapping, node))
        if isinstance(node, ast.Name):
            tokens[i] = new_token
        else:
            j = i
            while tokens[j].src != node.attr:
                # timid: if we see a parenthesis here, skip it
                if tokens[j].src == ')':
                    return
                j += 1
            tokens[i:j + 1] = [new_token]

    for i, token in reversed_enumerate(tokens):
        if not token.src:
            continue
        elif token.offset in visitor.bases_to_remove:
            _remove_base_class(tokens, i)
        elif token.offset in visitor.if_py3_blocks:
            if tokens[i].src == 'if':
                if_block = Block.find(tokens, i)
                if_block.dedent(tokens)
                del tokens[if_block.start:if_block.block]
            else:
                if_block = Block.find(tokens, _find_elif(tokens, i))
                if_block.replace_condition(tokens, [Token('NAME', 'else')])
        elif token.offset in visitor.if_py2_blocks_else:
            if tokens[i].src == 'if':
                if_block, else_block = _find_if_else_block(tokens, i)
                else_block.dedent(tokens)
                del tokens[if_block.start:else_block.block]
            else:
                j = _find_elif(tokens, i)
                if_block, else_block = _find_if_else_block(tokens, j)
                del tokens[if_block.start:else_block.start]
        elif token.offset in visitor.if_py3_blocks_else:
            if tokens[i].src == 'if':
                if_block, else_block = _find_if_else_block(tokens, i)
                if_block.dedent(tokens)
                del tokens[if_block.end:else_block.end]
                del tokens[if_block.start:if_block.block]
            else:
                j = _find_elif(tokens, i)
                if_block, else_block = _find_if_else_block(tokens, j)
                del tokens[if_block.end:else_block.end]
                if_block.replace_condition(tokens, [Token('NAME', 'else')])
        elif token.offset in visitor.metaclass_type_assignments:
            j = _find_end(tokens, i)
            del tokens[i:j + 1]
        elif token.offset in visitor.native_literals:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            if any(tok.name == 'NL' for tok in tokens[i:end]):
                continue
            if func_args:
                _replace_call(tokens, i, end, func_args, '{args[0]}')
            else:
                tokens[i:end] = [token._replace(name='STRING', src="''")]
        elif token.offset in visitor.six_type_ctx:
            _replace(i, SIX_TYPE_CTX_ATTRS, visitor.six_type_ctx[token.offset])
        elif token.offset in visitor.six_simple:
            _replace(i, SIX_SIMPLE_ATTRS, visitor.six_simple[token.offset])
        elif token.offset in visitor.six_remove_decorators:
            _remove_decorator(tokens, i)
        elif token.offset in visitor.six_b:
            j = _find_open_paren(tokens, i)
            if (
                    tokens[j + 1].name == 'STRING' and
                    _is_ascii(tokens[j + 1].src) and
                    tokens[j + 2].src == ')'
            ):
                func_args, end = _parse_call_args(tokens, j)
                _replace_call(tokens, i, end, func_args, SIX_B_TMPL)
        elif token.offset in visitor.six_iter:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            call = visitor.six_iter[token.offset]
            assert isinstance(call.func, (ast.Name, ast.Attribute))
            template = f'iter({_get_tmpl(SIX_CALLS, call.func)})'
            _replace_call(tokens, i, end, func_args, template)
        elif token.offset in visitor.six_calls:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            call = visitor.six_calls[token.offset]
            assert isinstance(call.func, (ast.Name, ast.Attribute))
            template = _get_tmpl(SIX_CALLS, call.func)
            if isinstance(call.args[0], _EXPR_NEEDS_PARENS):
                _replace_call(tokens, i, end, func_args, template, parens=(0,))
            else:
                _replace_call(tokens, i, end, func_args, template)
        elif token.offset in visitor.six_calls_int2byte:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            _replace_call(tokens, i, end, func_args, SIX_INT2BYTE_TMPL)
        elif token.offset in visitor.six_raise_from:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            _replace_call(tokens, i, end, func_args, RAISE_FROM_TMPL)
        elif token.offset in visitor.six_reraise:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            if len(func_args) == 1:
                tmpl = RERAISE_TMPL
            elif len(func_args) == 2:
                tmpl = RERAISE_2_TMPL
            else:
                tmpl = RERAISE_3_TMPL
            _replace_call(tokens, i, end, func_args, tmpl)
        elif token.offset in visitor.six_add_metaclass:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            metaclass = f'metaclass={_arg_str(tokens, *func_args[0])}'
            # insert `metaclass={args[0]}` into `class:`
            # search forward for the `class` token
            j = i + 1
            while tokens[j].src != 'class':
                j += 1
            class_token = j
            # then search forward for a `:` token, not inside a brace
            j = _find_block_start(tokens, j)
            last_paren = -1
            for k in range(class_token, j):
                if tokens[k].src == ')':
                    last_paren = k

            if last_paren == -1:
                tokens.insert(j, Token('CODE', f'({metaclass})'))
            else:
                insert = last_paren - 1
                while tokens[insert].name in NON_CODING_TOKENS:
                    insert -= 1
                if tokens[insert].src == '(':  # no bases
                    src = metaclass
                elif tokens[insert].src != ',':
                    src = f', {metaclass}'
                else:
                    src = f' {metaclass},'
                tokens.insert(insert + 1, Token('CODE', src))
            _remove_decorator(tokens, i)
        elif token.offset in visitor.six_with_metaclass:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            if len(func_args) == 1:
                tmpl = WITH_METACLASS_NO_BASES_TMPL
            elif len(func_args) == 2:
                base = _arg_str(tokens, *func_args[1])
                if base == 'object':
                    tmpl = WITH_METACLASS_NO_BASES_TMPL
                else:
                    tmpl = WITH_METACLASS_BASES_TMPL
            else:
                tmpl = WITH_METACLASS_BASES_TMPL
            _replace_call(tokens, i, end, func_args, tmpl)
        elif pep585_rewrite and token.offset in visitor.typing_builtin_renames:
            node = visitor.typing_builtin_renames[token.offset]
            _replace(i, PEP585_BUILTINS, node)
        elif token.offset in visitor.super_calls:
            i = _find_open_paren(tokens, i)
            call = visitor.super_calls[token.offset]
            victims = _victims(tokens, i, call, gen=False)
            del tokens[victims.starts[0] + 1:victims.ends[-1]]
        elif token.offset in visitor.encode_calls:
            i = _find_open_paren(tokens, i + 1)
            call = visitor.encode_calls[token.offset]
            victims = _victims(tokens, i, call, gen=False)
            del tokens[victims.starts[0] + 1:victims.ends[-1]]
        elif token.offset in visitor.io_open_calls:
            j = _find_open_paren(tokens, i)
            tokens[i:j] = [token._replace(name='NAME', src='open')]
        elif token.offset in visitor.mock_mock:
            j = _find_token(tokens, i + 1, 'mock')
            del tokens[i + 1:j + 1]
        elif token.offset in visitor.mock_absolute_imports:
            j = _find_token(tokens, i, 'mock')
            if (
                    j + 2 < len(tokens) and
                    tokens[j + 1].src == '.' and
                    tokens[j + 2].src == 'mock'
            ):
                j += 2
            src = 'from unittest import mock'
            tokens[i:j + 1] = [tokens[j]._replace(name='NAME', src=src)]
        elif token.offset in visitor.mock_relative_imports:
            j = _find_token(tokens, i, 'mock')
            if (
                    j + 2 < len(tokens) and
                    tokens[j + 1].src == '.' and
                    tokens[j + 2].src == 'mock'
            ):
                k = j + 2
            else:
                k = j
            src = 'unittest.mock'
            tokens[j:k + 1] = [tokens[j]._replace(name='NAME', src=src)]
        elif token.offset in visitor.open_mode_calls:
            j = _find_open_paren(tokens, i)
            func_args, end = _parse_call_args(tokens, j)
            mode = tokens_to_src(tokens[slice(*func_args[1])])
            mode_stripped = mode.strip().strip('"\'')
            if mode_stripped in U_MODE_REMOVE:
                del tokens[func_args[0][1]:func_args[1][1]]
            elif mode_stripped in U_MODE_REPLACE_R:
                new_mode = mode.replace('U', 'r')
                tokens[slice(*func_args[1])] = [Token('SRC', new_mode)]
            elif mode_stripped in U_MODE_REMOVE_U:
                new_mode = mode.replace('U', '')
                tokens[slice(*func_args[1])] = [Token('SRC', new_mode)]
            else:
                raise AssertionError(f'unreachable: {mode!r}')
        elif token.offset in visitor.os_error_alias_calls:
            j = _find_open_paren(tokens, i)
            tokens[i:j] = [token._replace(name='NAME', src='OSError')]
        elif token.offset in visitor.os_error_alias_simple:
            node = visitor.os_error_alias_simple[token.offset]
            _replace(i, collections.defaultdict(lambda: 'OSError'), node)
        elif token.offset in visitor.os_error_alias_excepts:
            line, utf8_byte_offset = token.line, token.utf8_byte_offset

            # find all the arg strs in the tuple
            except_index = i
            while tokens[except_index].src != 'except':
                except_index -= 1
            start = _find_open_paren(tokens, except_index)
            func_args, end = _parse_call_args(tokens, start)

            # save the exceptions and remove the block
            arg_strs = [_arg_str(tokens, *arg) for arg in func_args]
            del tokens[start:end]

            # rewrite the block without dupes
            args = []
            for arg in arg_strs:
                left, part, right = arg.partition('.')
                if (
                        left in visitor.OS_ERROR_ALIAS_MODULES and
                        part == '.' and
                        right == 'error'
                ):
                    args.append('OSError')
                elif (
                        left in visitor.OS_ERROR_ALIASES and
                        part == right == ''
                ):
                    args.append('OSError')
                elif (
                        left == 'error' and
                        part == right == '' and
                        (
                            'error' in visitor._from_imports['mmap'] or
                            'error' in visitor._from_imports['select'] or
                            'error' in visitor._from_imports['socket']
                        )
                ):
                    args.append('OSError')
                else:
                    args.append(arg)

            unique_args = tuple(collections.OrderedDict.fromkeys(args))

            if len(unique_args) > 1:
                joined = '({})'.format(', '.join(unique_args))
            elif tokens[start - 1].name != 'UNIMPORTANT_WS':
                joined = ' {}'.format(unique_args[0])
            else:
                joined = unique_args[0]

            new = Token('CODE', joined, line, utf8_byte_offset)
            tokens.insert(start, new)

            visitor.os_error_alias_excepts.discard(token.offset)
        elif token.offset in visitor.yield_from_fors:
            _replace_yield(tokens, i)
        elif (
                min_version >= (3, 8) and
                token.offset in visitor.no_arg_decorators
        ):
            i = _find_open_paren(tokens, i)
            j = _find_token(tokens, i, ')')
            del tokens[i:j + 1]

    return tokens_to_src(tokens)


def _simple_arg(arg: ast.expr) -> bool:
    return (
        isinstance(arg, ast.Name) or
        (isinstance(arg, ast.Attribute) and _simple_arg(arg.value)) or
        (
            isinstance(arg, ast.Call) and
            _simple_arg(arg.func) and
            not arg.args and not arg.keywords
        )
    )


def _starargs(call: ast.Call) -> bool:
    return (
        any(k.arg is None for k in call.keywords) or
        any(isinstance(a, ast.Starred) for a in call.args)
    )


def _format_params(call: ast.Call) -> Dict[str, str]:
    params = {}
    for i, arg in enumerate(call.args):
        params[str(i)] = _unparse(arg)
    for kwd in call.keywords:
        # kwd.arg can't be None here because we exclude starargs
        assert kwd.arg is not None
        params[kwd.arg] = _unparse(kwd.value)
    return params


class FindPy36Plus(ast.NodeVisitor):
    def __init__(self) -> None:
        self.fstrings: Dict[Offset, ast.Call] = {}
        self.named_tuples: Dict[Offset, ast.Call] = {}
        self.dict_typed_dicts: Dict[Offset, ast.Call] = {}
        self.kw_typed_dicts: Dict[Offset, ast.Call] = {}
        self._from_imports: Dict[str, Set[str]] = collections.defaultdict(set)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module in {'typing', 'typing_extensions'}:
            for name in node.names:
                if not name.asname:
                    self._from_imports[node.module].add(name.name)
        self.generic_visit(node)

    def _is_attr(self, node: ast.AST, mods: Set[str], name: str) -> bool:
        return (
            (
                isinstance(node, ast.Name) and
                node.id == name and
                any(name in self._from_imports[mod] for mod in mods)
            ) or
            (
                isinstance(node, ast.Attribute) and
                node.attr == name and
                isinstance(node.value, ast.Name) and
                node.value.id in mods
            )
        )

    def _parse(self, node: ast.Call) -> Optional[Tuple[DotFormatPart, ...]]:
        if not (
                isinstance(node.func, ast.Attribute) and
                isinstance(node.func.value, ast.Str) and
                node.func.attr == 'format' and
                all(_simple_arg(arg) for arg in node.args) and
                all(_simple_arg(k.value) for k in node.keywords) and
                not _starargs(node)
        ):
            return None

        try:
            return parse_format(node.func.value.s)
        except ValueError:
            return None

    def visit_Call(self, node: ast.Call) -> None:
        parsed = self._parse(node)
        if parsed is not None:
            params = _format_params(node)
            seen: Set[str] = set()
            i = 0
            for _, name, spec, _ in parsed:
                # timid: difficult to rewrite correctly
                if spec is not None and '{' in spec:
                    break
                if name is not None:
                    candidate, _, _ = name.partition('.')
                    # timid: could make the f-string longer
                    if candidate and candidate in seen:
                        break
                    # timid: bracketed
                    elif '[' in name:
                        break
                    seen.add(candidate)

                    key = candidate or str(i)
                    # their .format() call is broken currently
                    if key not in params:
                        break
                    if not candidate:
                        i += 1
            else:
                self.fstrings[_ast_to_offset(node)] = node

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if (
                # NT = ...("NT", ...)
                len(node.targets) == 1 and
                isinstance(node.targets[0], ast.Name) and
                isinstance(node.value, ast.Call) and
                len(node.value.args) >= 1 and
                isinstance(node.value.args[0], ast.Str) and
                node.targets[0].id == node.value.args[0].s and
                not _starargs(node.value)
        ):
            if (
                    self._is_attr(
                        node.value.func, {'typing'}, 'NamedTuple',
                    ) and
                    len(node.value.args) == 2 and
                    not node.value.keywords and
                    isinstance(node.value.args[1], (ast.List, ast.Tuple)) and
                    len(node.value.args[1].elts) > 0 and
                    all(
                        isinstance(tup, ast.Tuple) and
                        len(tup.elts) == 2 and
                        isinstance(tup.elts[0], ast.Str) and
                        tup.elts[0].s.isidentifier() and
                        tup.elts[0].s not in _KEYWORDS
                        for tup in node.value.args[1].elts
                    )
            ):
                self.named_tuples[_ast_to_offset(node)] = node.value
            elif (
                    self._is_attr(
                        node.value.func,
                        {'typing', 'typing_extensions'},
                        'TypedDict',
                    ) and
                    len(node.value.args) == 1 and
                    len(node.value.keywords) > 0
            ):
                self.kw_typed_dicts[_ast_to_offset(node)] = node.value
            elif (
                    self._is_attr(
                        node.value.func,
                        {'typing', 'typing_extensions'},
                        'TypedDict',
                    ) and
                    len(node.value.args) == 2 and
                    not node.value.keywords and
                    isinstance(node.value.args[1], ast.Dict) and
                    node.value.args[1].keys and
                    all(
                        isinstance(k, ast.Str) and
                        k.s.isidentifier() and
                        k.s not in _KEYWORDS
                        for k in node.value.args[1].keys
                    )
            ):
                self.dict_typed_dicts[_ast_to_offset(node)] = node.value

        self.generic_visit(node)


def _unparse(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        return ''.join((_unparse(node.value), '.', node.attr))
    elif isinstance(node, ast.Call):
        return '{}()'.format(_unparse(node.func))
    elif isinstance(node, ast.Subscript):
        if sys.version_info >= (3, 9):  # pragma: no cover (py39+)
            node_slice: ast.expr = node.slice
        elif isinstance(node.slice, ast.Index):  # pragma: no cover (<py39)
            node_slice = node.slice.value
        else:
            raise AssertionError(f'expected Slice: {ast.dump(node)}')
        if isinstance(node_slice, ast.Tuple):
            if len(node_slice.elts) == 1:
                slice_s = f'{_unparse(node_slice.elts[0])},'
            else:
                slice_s = ', '.join(_unparse(elt) for elt in node_slice.elts)
        else:
            slice_s = _unparse(node_slice)
        return '{}[{}]'.format(_unparse(node.value), slice_s)
    elif isinstance(node, ast.Str):
        return repr(node.s)
    elif isinstance(node, ast.Ellipsis):
        return '...'
    elif isinstance(node, ast.List):
        return '[{}]'.format(', '.join(_unparse(elt) for elt in node.elts))
    elif isinstance(node, ast.NameConstant):
        return repr(node.value)
    else:
        raise NotImplementedError(ast.dump(node))


ENDS_ON_NAMED_UNICODE_RE = re.compile(r'(?<!\\)(?:\\\\)*\\N$')


def _to_fstring(src: str, call: ast.Call) -> str:
    params = _format_params(call)

    parts = []
    i = 0
    for s, name, spec, conv in parse_format('f' + src):
        if name is not None:
            k, dot, rest = name.partition('.')
            name = ''.join((params[k or str(i)], dot, rest))
            if not k:  # named and auto params can be in different orders
                i += 1
        parts.append((s, name, spec, conv))
    return unparse_parsed_string(parts)


def _replace_typed_class(
        tokens: List[Token],
        i: int,
        call: ast.Call,
        types: Dict[str, ast.expr],
) -> None:
    if i > 0 and tokens[i - 1].name in {'INDENT', UNIMPORTANT_WS}:
        indent = f'{tokens[i - 1].src}{" " * 4}'
    else:
        indent = ' ' * 4

    # NT = NamedTuple("nt", [("a", int)])
    # ^i                                 ^end
    end = i + 1
    while end < len(tokens) and tokens[end].name != 'NEWLINE':
        end += 1

    attrs = '\n'.join(f'{indent}{k}: {_unparse(v)}' for k, v in types.items())
    src = f'class {tokens[i].src}({_unparse(call.func)}):\n{attrs}'
    tokens[i:end] = [Token('CODE', src)]


def _fix_py36_plus(contents_text: str) -> str:
    try:
        ast_obj = ast_parse(contents_text)
    except SyntaxError:
        return contents_text

    visitor = FindPy36Plus()
    visitor.visit(ast_obj)

    if not any((
            visitor.fstrings,
            visitor.named_tuples,
            visitor.dict_typed_dicts,
            visitor.kw_typed_dicts,
    )):
        return contents_text

    try:
        tokens = src_to_tokens(contents_text)
    except tokenize.TokenError:  # pragma: no cover (bpo-2180)
        return contents_text
    for i, token in reversed_enumerate(tokens):
        if token.offset in visitor.fstrings:
            node = visitor.fstrings[token.offset]

            paren = i + 3
            if tokens_to_src(tokens[i + 1:paren + 1]) != '.format(':
                continue

            # we don't actually care about arg position, so we pass `node`
            victims = _victims(tokens, paren, node, gen=False)
            end = victims.ends[-1]
            # if it spans more than one line, bail
            if tokens[end].line != token.line:
                continue

            try:
                tokens[i] = token._replace(src=_to_fstring(token.src, node))
                del tokens[i + 1:end + 1]
            except KeyError:
                pass
        elif token.offset in visitor.named_tuples and token.name == 'NAME':
            call = visitor.named_tuples[token.offset]
            types: Dict[str, ast.expr] = {
                tup.elts[0].s: tup.elts[1]  # type: ignore  # (checked above)
                for tup in call.args[1].elts  # type: ignore  # (checked above)
            }
            _replace_typed_class(tokens, i, call, types)
        elif token.offset in visitor.kw_typed_dicts and token.name == 'NAME':
            call = visitor.kw_typed_dicts[token.offset]
            types = {
                arg.arg: arg.value  # type: ignore  # (checked above)
                for arg in call.keywords
            }
            _replace_typed_class(tokens, i, call, types)
        elif token.offset in visitor.dict_typed_dicts and token.name == 'NAME':
            call = visitor.dict_typed_dicts[token.offset]
            types = {
                k.s: v  # type: ignore  # (checked above)
                for k, v in zip(
                    call.args[1].keys,  # type: ignore  # (checked above)
                    call.args[1].values,  # type: ignore  # (checked above)
                )
            }
            _replace_typed_class(tokens, i, call, types)

    return tokens_to_src(tokens)


def _fix_file(filename: str, args: argparse.Namespace) -> int:
    if filename == '-':
        contents_bytes = sys.stdin.buffer.read()
    else:
        with open(filename, 'rb') as fb:
            contents_bytes = fb.read()

    try:
        contents_text_orig = contents_text = contents_bytes.decode()
    except UnicodeDecodeError:
        print(f'{filename} is non-utf-8 (not supported)')
        return 1

    contents_text = _fix_py2_compatible(contents_text)
    contents_text = _fix_tokens(contents_text, min_version=args.min_version)
    if not args.keep_percent_format:
        contents_text = _fix_percent_format(contents_text)
    if args.min_version >= (3,):
        contents_text = _fix_py3_plus(
            contents_text, args.min_version, args.keep_mock,
        )
    if args.min_version >= (3, 6):
        contents_text = _fix_py36_plus(contents_text)

    if filename == '-':
        print(contents_text, end='')
    elif contents_text != contents_text_orig:
        print(f'Rewriting {filename}', file=sys.stderr)
        with open(filename, 'w', encoding='UTF-8', newline='') as f:
            f.write(contents_text)

    if args.exit_zero_even_if_changed:
        return 0
    else:
        return contents_text != contents_text_orig


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('filenames', nargs='*')
    parser.add_argument('--exit-zero-even-if-changed', action='store_true')
    parser.add_argument('--keep-percent-format', action='store_true')
    parser.add_argument('--keep-mock', action='store_true')
    parser.add_argument(
        '--py3-plus', '--py3-only',
        action='store_const', dest='min_version', default=(2, 7), const=(3,),
    )
    parser.add_argument(
        '--py36-plus',
        action='store_const', dest='min_version', const=(3, 6),
    )
    parser.add_argument(
        '--py37-plus',
        action='store_const', dest='min_version', const=(3, 7),
    )
    parser.add_argument(
        '--py38-plus',
        action='store_const', dest='min_version', const=(3, 8),
    )
    parser.add_argument(
        '--py39-plus',
        action='store_const', dest='min_version', const=(3, 9),
    )
    args = parser.parse_args(argv)

    ret = 0
    for filename in args.filenames:
        ret |= _fix_file(filename, args)
    return ret


if __name__ == '__main__':
    exit(main())
