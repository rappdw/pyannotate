"""Fixer that inserts mypy annotations into all methods.

This transforms e.g.

  def foo(self, bar, baz=12):
      return bar + baz

into

  def foo(self, bar, baz=12):
      # type: (Any, int) -> Any            # noqa: F821
      return bar + baz

It does not do type inference but it recognizes some basic default
argument values such as numbers and strings (and assumes their type
implies the argument type).

It also uses some basic heuristics to decide whether to ignore the
first argument:

  - always if it's named 'self'
  - if there's a @classmethod decorator

Finally, it knows that __init__() is supposed to return None.
"""

from __future__ import print_function

import os
import re

from lib2to3.fixer_base import BaseFix
from lib2to3.fixer_util import syms, touch_import
from lib2to3.patcomp import compile_pattern
from lib2to3.pgen2 import token
from lib2to3.pytree import Leaf, Node


class FixAnnotate(BaseFix):

    # This fixer is compatible with the bottom matcher.
    BM_compatible = True

    # This fixer shouldn't run by default.
    explicit = True

    # The pattern to match.
    PATTERN = """
              funcdef< 'def' name=any parameters< '(' [args=any] ')' > ':' suite=any+ >
              """

    _maxfixes = os.getenv('MAXFIXES')
    counter = None if not _maxfixes else int(_maxfixes)

    def transform(self, node, results):
        if FixAnnotate.counter is not None:
            if FixAnnotate.counter <= 0:
                return
        suite = results['suite']
        children = suite[0].children

        # NOTE: I've reverse-engineered the structure of the parse tree.
        # It's always a list of nodes, the first of which contains the
        # entire suite.  Its children seem to be:
        #
        #   [0] NEWLINE
        #   [1] INDENT
        #   [2...n-2] statements (the first may be a docstring)
        #   [n-1] DEDENT
        #
        # Comments before the suite are part of the INDENT's prefix.
        #
        # "Compact" functions (e.g. "def foo(x, y): return max(x, y)")
        # have a different structure that isn't matched by PATTERN.

        ## print('-'*60)
        ## print(node)
        ## for i, ch in enumerate(children):
        ##     print(i, repr(ch.prefix), repr(ch))

        # Check if there's already an annotation.
        for ch in children:
            if ch.prefix.lstrip().startswith('# type:'):
                return  # There's already a # type: comment here; don't change anything.

        # Compute the annotation
        annot = self.make_annotation(node, results)
        if annot is None:
            return
        if 'type_hints' in self.options and self.options['type_hints']:
            self.insert_python_3_annotation(node, results, annot)
        else:
            self.insert_python_2_annotation(node, results, children, annot)

    def get_parm_list(self, parm_list_children, node):
        # we have a couple cases we expect here, in both cases, the list should begin with leaf '(' and
        # end with leaf ')' and be length 3
        parms = []
        if len(parm_list_children) == 2 and parm_list_children[0].type == token.LPAR \
                and parm_list_children[1].type == token.RPAR:
            return parms
        if len(parm_list_children) != 3 or \
                parm_list_children[0].type != token.LPAR or \
                parm_list_children[2].type != token.RPAR:
            self.log_message("%s:%d: Unexpected AST: Expecting '(args, ...)' got something else..." %
                             (self.filename, node.get_lineno()))
            return parms
        if isinstance(parm_list_children[1], Node):
            skip = False
            working_parm = None
            for parm in parm_list_children[1].children:
                if skip:
                    if parm.type == token.COMMA:
                        skip = False
                        if working_parm:
                            parms.append(working_parm)
                            working_parm = None
                else:
                    if parm.type == token.NAME:
                        working_parm = parm
                    elif parm.type == token.COLON:
                        # already type_hinted, we'll ignore although we could check
                        # type equivalency...
                        working_parm = None
                        skip = True
                    elif parm.type == token.EQUAL:
                        skip = True
                    elif parm.type == token.COMMA:
                        if working_parm:
                            parms.append(working_parm)
                            working_parm = None
            if working_parm:
                parms.append(working_parm)
        elif isinstance(parm_list_children[1], Leaf):
            parms.append(parm_list_children[1])
        return parms

    def insert_python_3_annotation(self, node, results, annot):
        """
        Inserts Python 3.5 type hinting (PEP 484).

        This implementation take a "short cut" and just modifies the parameter name to
        append the type hint information, rather than constructing an appropriate AST.
        Nevertheless it acheives the desired result

        :param node: node in AST that represents the function
        :param results: results passed into the transform function
        :param annot: the annotations
        :return: None (modifies the AST in place)
        """
        # Insert Python 3.5 type hinting
        argtypes, restype, skipped_first = annot
        name = results['name']
        parm_list = name.next_sibling
        if not isinstance(parm_list, Node):
            self.log_message("%s:%d: %s: Unexpected AST. Parameter list is of type: %s." %
                             (self.filename, node.get_lineno(), name, type(parm_list)))
            return
        parms = self.get_parm_list(parm_list.children, node)
        parm_count = len(parms)
        if parm_count > 0:
            if skipped_first:
                parms = parms[1:]
                parm_count -= 1
            if parm_count != len(argtypes):
                self.log_message("%s:%d: Unexpected parameter count: %s: %d skipping (parameters: %s -- annotations: %s)" %
                                 (self.filename, node.get_lineno(), name, parm_count, parms, argtypes))
                return
            for parm, annotation in zip(parms, argtypes):
                parm.value = '%s: %s' % (parm.value, annotation)
                parm.changed()

        rtn = parm_list.next_sibling
        if isinstance(parm_list, Node):
            if rtn.type == token.COLON and not name.value.startswith('__'):
                rtn.value = ' -> %s:' % restype
                rtn.changed()
        else:
            self.log_message("%s:%d: %s: Unexpected AST. First node after param_list is of type: %s." %
                             (self.filename, node.get_lineno(), name, type(rtn)))

        if FixAnnotate.counter is not None:
            FixAnnotate.counter -= 1

        # Also add 'from typing import Any' at the top if needed.
        self.patch_imports(argtypes + [restype], node)

    def insert_python_2_annotation(self, node, results, children, annot):
        # Insert '# type: {annot}' comment.
        # For reference, see lib2to3/fixes/fix_tuple_params.py in stdlib.
        if len(children) >= 2 and children[1].type == token.INDENT:
            argtypes, restype, _ = annot
            degen_str = '(...) -> %s' % restype
            short_str = '(%s) -> %s' % (', '.join(argtypes), restype)
            if (len(short_str) > 64 or len(argtypes) > 5) and len(short_str) > len(degen_str):
                self.insert_long_form(node, results, argtypes)
                annot_str = degen_str
            else:
                annot_str = short_str
            children[1].prefix = '%s# type: %s\n%s' % (children[1].value, annot_str,
                                                       children[1].prefix)
            children[1].changed()
            if FixAnnotate.counter is not None:
                FixAnnotate.counter -= 1

            # Also add 'from typing import Any' at the top if needed.
            self.patch_imports(argtypes + [restype], node)
        else:
            self.log_message("%s:%d: cannot insert annotation for one-line function" %
                             (self.filename, node.get_lineno()))

    def insert_long_form(self, node, results, argtypes):
        argtypes = list(argtypes)  # We destroy it
        args = results['args']
        if isinstance(args, Node):
            children = args.children
        elif isinstance(args, Leaf):
            children = [args]
        else:
            children = []
        # Interpret children according to the following grammar:
        # (('*'|'**')? NAME ['=' expr] ','?)*
        flag = False  # Set when the next leaf should get a type prefix
        indent = ''  # Will be set by the first child

        def set_prefix(child):
            if argtypes:
                arg = argtypes.pop(0).lstrip('*')
            else:
                arg = 'Any'  # Somehow there aren't enough args
            if not arg:
                # Skip self (look for 'check_self' below)
                prefix = child.prefix.rstrip()
            else:
                prefix = '  # type: ' + arg
                old_prefix = child.prefix.strip()
                if old_prefix:
                    assert old_prefix.startswith('#')
                    prefix += '  ' + old_prefix
            child.prefix = prefix + '\n' + indent

        check_self = self.is_method(node)
        for child in children:
            if check_self and isinstance(child, Leaf) and child.type == token.NAME:
                check_self = False
                if child.value in ('self', 'cls'):
                    argtypes.insert(0, '')
            if not indent:
                indent = ' ' * child.column
            if isinstance(child, Leaf) and child.value == ',':
                flag = True
            elif isinstance(child, Leaf) and flag:
                set_prefix(child)
                flag = False
        # Find the ')' and insert a prefix before it too.
        parameters = args.parent
        close_paren = parameters.children[-1]
        assert close_paren.type == token.RPAR, close_paren
        set_prefix(close_paren)
        assert not argtypes, argtypes

    def patch_imports(self, types, node):
        for typ in types:
            if 'Any' in typ:
                touch_import('typing', 'Any', node)
                break

    def make_annotation(self, node, results):
        name = results['name']
        assert isinstance(name, Leaf), repr(name)
        assert name.type == token.NAME, repr(name)
        decorators = self.get_decorators(node)
        is_method = self.is_method(node)
        if name.value == '__init__' or not self.has_return_exprs(node):
            restype = 'None'
        else:
            restype = 'Any'
        args = results.get('args')
        argtypes = []
        if isinstance(args, Node):
            children = args.children
        elif isinstance(args, Leaf):
            children = [args]
        else:
            children = []
        # Interpret children according to the following grammar:
        # (('*'|'**')? NAME ['=' expr] ','?)*
        stars = inferred_type = ''
        in_default = False
        at_start = True
        skipped_first = False
        type_hints = 'type_hints' in self.options and self.options['type_hints']
        for child in children:
            if isinstance(child, Leaf):
                if child.value in ('*', '**'):
                    if not type_hints:
                        stars += child.value
                elif child.type == token.NAME and not in_default:
                    if not is_method or not at_start or 'staticmethod' in decorators:
                        inferred_type = 'Any'
                    else:
                        # Always skip the first argument if it's named 'self'.
                        # Always skip the first argument of a class method.
                        if  child.value == 'self' or 'classmethod' in decorators:
                            skipped_first = True
                        else:
                            inferred_type = 'Any'
                elif child.value == '=':
                    in_default = True
                elif in_default and child.value != ',':
                    if child.type == token.NUMBER:
                        if re.match(r'\d+[lL]?$', child.value):
                            inferred_type = 'int'
                        else:
                            inferred_type = 'float'  # TODO: complex?
                    elif child.type == token.STRING:
                        if child.value.startswith(('u', 'U')):
                            inferred_type = 'unicode'
                        else:
                            inferred_type = 'str'
                    elif child.type == token.NAME and child.value in ('True', 'False'):
                        inferred_type = 'bool'
                elif child.value == ',':
                    if inferred_type:
                        argtypes.append(stars + inferred_type)
                    # Reset
                    stars = inferred_type = ''
                    in_default = False
                    at_start = False
        if inferred_type:
            argtypes.append(stars + inferred_type)
        return argtypes, restype, skipped_first

    # The parse tree has a different shape when there is a single
    # decorator vs. when there are multiple decorators.
    DECORATED = "decorated< (d=decorator | decorators< dd=decorator+ >) funcdef >"
    decorated = compile_pattern(DECORATED)

    def get_decorators(self, node):
        """Return a list of decorators found on a function definition.

        This is a list of strings; only simple decorators
        (e.g. @staticmethod) are returned.

        If the function is undecorated or only non-simple decorators
        are found, return [].
        """
        if node.parent is None:
            return []
        results = {}
        if not self.decorated.match(node.parent, results):
            return []
        decorators = results.get('dd') or [results['d']]
        decs = []
        for d in decorators:
            for child in d.children:
                if isinstance(child, Leaf) and child.type == token.NAME:
                    decs.append(child.value)
        return decs

    def is_method(self, node):
        """Return whether the node occurs (directly) inside a class."""
        node = node.parent
        while node is not None:
            if node.type == syms.classdef:
                return True
            if node.type == syms.funcdef:
                return False
            node = node.parent
        return False

    RETURN_EXPR = "return_stmt< 'return' any >"
    return_expr = compile_pattern(RETURN_EXPR)

    def has_return_exprs(self, node):
        """Traverse the tree below node looking for 'return expr'.

        Return True if at least 'return expr' is found, False if not.
        (If both 'return' and 'return expr' are found, return True.)
        """
        results = {}
        if self.return_expr.match(node, results):
            return True
        for child in node.children:
            if child.type not in (syms.funcdef, syms.classdef):
                if self.has_return_exprs(child):
                    return True
        return False

    YIELD_EXPR = "yield_expr< 'yield' [any] >"
    yield_expr = compile_pattern(YIELD_EXPR)

    def is_generator(self, node):
        """Traverse the tree below node looking for 'yield [expr]'."""
        results = {}
        if self.yield_expr.match(node, results):
            return True
        for child in node.children:
            if child.type not in (syms.funcdef, syms.classdef):
                if self.is_generator(child):
                    return True
        return False
