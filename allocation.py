"""Mentor allocation engine (pure Python, no database, easy to test).

Idea in plain words
-------------------
* A teacher may only mentor a class they TEACH (eligibility).
* A teacher may never get more mentees than their capacity.
* Among all valid ways to allocate, we pick the one that keeps the BUSIEST
  mentor as light as possible (min-max fairness), counting mentees they
  already have.

How: we model it as a max-flow problem
    source -> class (number of waiting students)
           -> eligible teacher (only if teacher teaches that class)
           -> sink (teacher's remaining room under a "level" L)
and binary-search the smallest level L that still places as many students
as the hard limits allow. Students that still cannot be placed are reported
with a reason - they are never forced onto an ineligible or full mentor.
"""
from collections import deque

NO_ELIGIBLE = "NO_ELIGIBLE_MENTOR"
AT_CAPACITY = "ALL_ELIGIBLE_MENTORS_AT_CAPACITY"


def _max_flow(graph, s, t):
    """Edmonds-Karp on a dict-of-dict residual graph. Mutates `graph`."""
    flow = 0
    while True:
        parent = {s: None}
        q = deque([s])
        while q and t not in parent:
            u = q.popleft()
            for v, cap in graph[u].items():
                if cap > 0 and v not in parent:
                    parent[v] = u
                    q.append(v)
        if t not in parent:
            return flow
        # bottleneck
        b, v = float("inf"), t
        while parent[v] is not None:
            b = min(b, graph[parent[v]][v])
            v = parent[v]
        v = t
        while parent[v] is not None:
            u = parent[v]
            graph[u][v] -= b
            graph[v][u] = graph[v].get(u, 0) + b
            v = u
        flow += b


def _build(pending_counts, eligible, load, caps, level):
    g = {"S": {}, "T": {}}
    for c, n in pending_counts.items():
        g.setdefault(("C", c), {})
        g["S"][("C", c)] = n
        g[("C", c)].setdefault("S", 0)
        for t in eligible.get(c, ()):
            g.setdefault(("T", t), {})
            g[("C", c)][("T", t)] = n
            g[("T", t)].setdefault(("C", c), 0)
    for node in list(g):
        if isinstance(node, tuple) and node[0] == "T":
            t = node[1]
            room = max(0, min(level, caps[t]) - load.get(t, 0))
            g[node]["K"] = room
            g.setdefault("K", {})[node] = 0
    g.setdefault("K", {})
    return g


def allocate(pending, eligible, load, caps):
    """
    pending : {class_key: [student_id, ...]}   students who still need a mentor
    eligible: {class_key: set(teacher)}        teachers who teach that class
    load    : {teacher: current mentee count}
    caps    : {teacher: max mentees}           hard limit per teacher
    returns : (assignments {student: teacher}, unallocated {student: reason})
    """
    assignments, unallocated = {}, {}
    workable = {}
    for c, studs in pending.items():
        if not studs:
            continue
        if not eligible.get(c):
            for s in studs:
                unallocated[s] = NO_ELIGIBLE
        else:
            workable[c] = sorted(studs)
    if not workable:
        return assignments, unallocated

    counts = {c: len(v) for c, v in workable.items()}
    teachers = sorted({t for c in workable for t in eligible[c]})
    max_cap = max(caps[t] for t in teachers)
    total = sum(counts.values())

    # Water-filling: raise every mentor's ceiling one step at a time (L = 1, 2, 3...)
    # and keep the flow found so far. Light mentors fill up first, so loads stay
    # level; no mentor ever goes above their own capacity.
    g = _build(counts, eligible, load, caps, 0)
    placed, prev_room = 0, {t: 0 for t in teachers}
    for level in range(1, max_cap + 1):
        for t in teachers:
            room = max(0, min(level, caps[t]) - load.get(t, 0))
            g[("T", t)]["K"] += room - prev_room[t]
            prev_room[t] = room
        placed += _max_flow(g, "S", "K")
        if placed == total:
            break

    # read how many students each (class, teacher) pair received
    for c, studs in workable.items():
        idx = 0
        for t in sorted(eligible[c]):
            # flow on edge = original capacity (n) - residual
            got = counts[c] - g[("C", c)][("T", t)]
            for s in studs[idx:idx + got]:
                assignments[s] = t
            idx += got
        for s in studs[idx:]:
            unallocated[s] = AT_CAPACITY
    return assignments, unallocated
