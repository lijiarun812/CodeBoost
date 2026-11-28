"""Run one EffiBench candidate in a separate process. Input/output are JSON."""
import ast
import json
import sys
import time


def main():
    req = json.load(sys.stdin)
    ns = {}
    exec('from typing import *\nfrom collections import *\nfrom functools import *\n'
         'from itertools import *\nfrom math import *\nfrom bisect import *\n'
         'from heapq import *\nfrom copy import *\nfrom operator import *', ns)
    exec('class ListNode:\n def __init__(self, val=0, next=None):\n  self.val, self.next = val, next\n'
         'class TreeNode:\n def __init__(self, val=0, left=None, right=None):\n'
         '  self.val, self.left, self.right = val, left, right', ns)
    code = req['code']
    if '```' in code:
        import re
        match = re.search(r'```(?:python)?\s*\n(.*?)```', code, re.S | re.I)
        if match:
            code = match.group(1)
    exec(compile(code, '<candidate>', 'exec'), ns)
    cls = ns.get('Solution')
    if cls is None:
        raise ValueError('class Solution missing')
    tree = ast.parse(req['test_case'])
    if not tree.body or any(not isinstance(n, ast.Assert) for n in tree.body):
        raise ValueError('test suite is not a pure assertion list')
    ns['solution'] = cls()
    compiled_tests = []
    for index, node in enumerate(tree.body):
        statement = ast.Expression(node.test)
        compiled_tests.append((compile(ast.fix_missing_locations(statement), '<test>', 'eval'),
                               ast.get_source_segment(req['test_case'], node)[:500]))
    repeats = max(1, int(req.get('repeats', 1)))
    timings = [0] * len(compiled_tests)
    for _ in range(repeats):
        for index, (compiled, source) in enumerate(compiled_tests):
            start = time.perf_counter_ns()
            passed = eval(compiled, ns)
            timings[index] += time.perf_counter_ns() - start
            if not passed:
                print(json.dumps({'correct': False, 'failed_index': index,
                                  'failed_test': source}))
                return
    timings = [{'index': i, 'ns': ns_elapsed, 'test': compiled_tests[i][1]}
               for i, ns_elapsed in enumerate(timings)]
    timings.sort(key=lambda x: x['ns'], reverse=True)
    print(json.dumps({'correct': True, 'total_ns': sum(x['ns'] for x in timings),
                      'slowest': timings[:5], 'n_tests': len(timings), 'repeats': repeats}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'correct': False, 'error': f'{type(exc).__name__}: {str(exc)[:300]}'}))
