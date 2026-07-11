"""Pattern-matrix usefulness (Maranget) — exhaustiveness + unreachable-arm detection.

Given the checker-resolved patterns of a `match`, this answers "is there a value no arm
covers?" (exhaustiveness) and "is this arm reachable given the ones above it?" (redundancy),
returning a concrete missing-pattern witness for the error message. Scoped to v1 patterns:
wildcard/binding (both cover everything), literal Int/Str/Bool, and variants with sub-patterns
(tuples in M5 drop in as one k-ary constructor with no change). No guards, no or-patterns.

Written vector-general over pattern matrices (DESIGN.md §2.7, Fable ruling 5).
"""

from __future__ import annotations

from tantu.ttypes import EnumT, TupleT, BOOL, INT, STR, subst

TUPLE_KEY = "()"     # the single constructor key for a tuple sort


# -- simplified patterns fed to the algorithm --------------------------------

class Wild:
    """Wildcard / binding — covers any value."""


class Ctor:
    __slots__ = ("key", "subs")

    def __init__(self, key, subs: list) -> None:
        self.key = key          # variant tag (int) | literal value (int/str/bool)
        self.subs = subs        # list[Wild|Ctor]


WILD = Wild()


def sort_of(ty) -> tuple:
    """The 'sort' of a column: its constructor space, used for completeness."""
    if isinstance(ty, EnumT):
        return ("enum", ty.name, ty.args)
    if isinstance(ty, TupleT):
        return ("tuple", tuple(sort_of(t) for t in ty.items))
    if ty == BOOL:
        return ("bool",)
    if ty == INT:
        return ("int",)
    if ty == STR:
        return ("str",)
    return ("opaque",)          # no finite signature (never complete)


def _signature(sort, registry):
    """The full set of constructor keys for a sort, or None if infinite/unknown."""
    if sort[0] == "enum":
        edef = registry.get(sort[1])
        if edef is None:            # foreign/unknown enum: no definition → never complete
            return None
        return {v.tag for v in edef.variants}
    if sort[0] == "bool":
        return {True, False}
    if sort[0] == "tuple":
        return {TUPLE_KEY}
    return None


def _field_sorts(sort, key, registry) -> list:
    if sort[0] == "enum":
        _, name, args = sort
        edef = registry[name]
        mapping = dict(zip(edef.type_params, args))
        return [sort_of(subst(p, mapping)) for p in edef.variants[key].payload]
    if sort[0] == "tuple":
        return list(sort[1])
    return []                    # literals/bools are nullary


def _arity(sort, key, registry) -> int:
    if sort[0] == "enum":
        return len(registry[sort[1]].variants[key].payload)
    if sort[0] == "tuple":
        return len(sort[1])
    return 0


def _specialize(matrix, key, arity):
    out = []
    for row in matrix:
        head = row[0]
        if isinstance(head, Ctor) and head.key == key:
            out.append(head.subs + row[1:])
        elif isinstance(head, Wild):
            out.append([WILD] * arity + row[1:])
    return out


def _default(matrix):
    return [row[1:] for row in matrix if isinstance(row[0], Wild)]


def _missing(sort, heads, registry) -> str:
    if sort[0] == "enum":
        edef = registry.get(sort[1])
        if edef is None:            # foreign/unknown enum: no variant names to offer
            return "_"
        for v in edef.variants:
            if v.tag not in heads:
                inner = ", ".join("_" for _ in v.payload)
                return f"{v.name}({inner})" if v.payload else v.name
    if sort[0] == "tuple":
        return "(" + ", ".join("_" for _ in sort[1]) + ")"
    if sort[0] == "bool":
        return "false" if False not in heads else "true"
    return "_"


def usefulness(matrix, q, sorts, registry):
    """Return None if `q` is redundant w.r.t. `matrix`, else a witness (list of strings)."""
    if not q:
        return None if matrix else []
    q0, qrest = q[0], q[1:]
    sort0 = sorts[0]
    if isinstance(q0, Ctor):
        arity = len(q0.subs)
        spec = _specialize(matrix, q0.key, arity)
        w = usefulness(spec, q0.subs + qrest, _field_sorts(sort0, q0.key, registry) + sorts[1:],
                       registry)
        if w is None:
            return None
        return [_render_named(sort0, q0.key, w[:arity], registry)] + w[arity:]
    heads = {row[0].key for row in matrix if isinstance(row[0], Ctor)}
    sig = _signature(sort0, registry)
    if sig is not None and sig <= heads:               # complete signature
        for key in sig:
            arity = _arity(sort0, key, registry)
            spec = _specialize(matrix, key, arity)
            w = usefulness(spec, [WILD] * arity + qrest,
                           _field_sorts(sort0, key, registry) + sorts[1:], registry)
            if w is not None:
                return [_render_named(sort0, key, w[:arity], registry)] + w[arity:]
        return None
    w = usefulness(_default(matrix), qrest, sorts[1:], registry)   # incomplete → default matrix
    if w is None:
        return None
    return [_missing(sort0, heads, registry)] + w


def _render_named(sort, key, sub_witnesses, registry) -> str:
    if sort[0] == "enum":
        vname = registry[sort[1]].variants[key].name
        return f"{vname}({', '.join(sub_witnesses)})" if sub_witnesses else vname
    if sort[0] == "tuple":
        return "(" + ", ".join(sub_witnesses) + ")"
    if sort[0] == "bool":
        return "true" if key else "false"
    return repr(key) if sort[0] == "str" else str(key)


def missing_witness(rows, scrut_sort, registry) -> str | None:
    """Return a missing-pattern witness (string) if the match is not exhaustive, else None."""
    w = usefulness(rows, [WILD], [scrut_sort], registry)
    return w[0] if w is not None else None


def is_useful(prev_rows, row, scrut_sort, registry) -> bool:
    """Is `row` reachable given the arms above it? (False ⇒ unreachable/redundant arm.)"""
    return usefulness(prev_rows, row, [scrut_sort], registry) is not None
