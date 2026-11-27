"""Approximate line hotspots for one expensive test, isolated in a child process."""
import ast
import json
import re
import sys
import time


def main():
    req = json.load(sys.stdin)
    code = req['code']
    match = re.search(r'```(?:python)?\s*\n(.*?)```', code, re.S | re.I)
    if match:
        code = match.group(1)
    ns = {}
    exec('import random, string, re, math, itertools, collections\n'
         'from typing import *\nfrom collections import *\nfrom functools import *\n'
         'from itertools import *\nfrom math import *\nfrom bisect import *\n'
         'from heapq import *\nfrom copy import *\nfrom operator import *', ns)
    exec(compile(code, '<candidate>', 'exec'), ns)
    ns['solution'] = ns['Solution']()
    node = ast.parse(req['test_case']).body[req['index']]
    test = compile(ast.fix_missing_locations(ast.Expression(node.test)), '<test>', 'eval')
    totals = {}
    hits = {}
    last = [None, None]

    def trace(frame, event, arg):
        if frame.f_code.co_filename != '<candidate>':
            return trace
        now = time.perf_counter_ns()
        if last[0] is not None:
            totals[last[0]] = totals.get(last[0], 0) + now - last[1]
        if event == 'line':
            hits[frame.f_lineno] = hits.get(frame.f_lineno, 0) + 1
        last[0], last[1] = frame.f_lineno if event != 'return' else None, now
        return trace

    profile_start = time.perf_counter_ns()
    sys.settrace(trace)
    try:
        passed = eval(test, ns)
    finally:
        sys.settrace(None)
    profile_wall_ns = time.perf_counter_ns() - profile_start
    if not passed:
        raise ValueError('test failed while profiling')
    source = code.splitlines()
    lines = [{'line': n, 'source': source[n - 1].strip(), 'hits': hits.get(n, 0),
              'profile_ns': elapsed}
             for n, elapsed in sorted(totals.items(), key=lambda x: -x[1])[:5]
             if 1 <= n <= len(source)]
    print(json.dumps({'lines': lines, 'profile_wall_ns': profile_wall_ns,
                      'profiled_test_index': req['index']}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': f'{type(exc).__name__}: {str(exc)[:200]}'}))
