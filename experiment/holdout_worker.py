"""Generate deterministic extra tests from the official EffiBench generator."""
import ast
import contextlib
import io
import json
import random
import sys


def main():
    row = json.load(sys.stdin)
    random.seed(20261127)
    ns = {}
    exec('import random, string, re, math, itertools, collections\n'
         'from typing import *\nfrom collections import *\nfrom functools import *\n'
         'from itertools import *\nfrom math import *\nfrom bisect import *\n'
         'from heapq import *\nfrom copy import *\nfrom operator import *', ns)
    exec(row['canonical_solution'], ns)
    with contextlib.redirect_stdout(io.StringIO()):
        exec(row['test_case_generator'], ns)
        generated = ns['test_generated_test_cases'](30)
    if not isinstance(generated, list) or not generated:
        raise ValueError('generator returned no tests')
    tests = []
    for test in generated:
        if not isinstance(test, str):
            raise ValueError('test is not text')
        tree = ast.parse(test)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assert):
            raise ValueError('test is not one assertion')
        tests.append(test)
    print(json.dumps({'tests': tests, 'seed': 20261127}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': f'{type(exc).__name__}: {str(exc)[:300]}'}))
