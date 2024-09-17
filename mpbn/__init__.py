"""
This module provides a simple implementation of Most Permissive Boolean Networks
(MPBNs) for computing reachability properties, attractors, reachable attractors.
Attractors of MPBNs are the *minimal* trap spaces of the underlying Boolean maps.
See http://dx.doi.org/10.1101/2020.03.22.998377 and https://arxiv.org/abs/1808.10240 for technical details.

It relies on clingo Answer-Set Programming solver
(https://github.com/potassco/clingo).

Examples are available at https://nbviewer.jupyter.org/github/pauleve/mpbn/tree/master/examples/

Quick example:

>>> mbn = mpbn.MPBooleanNetwork({
        "a": "!b",
        "b": "!a",
        "c": "!a & b"})
>>> list(mbn.attractors()) # minimal trap spaces
[{'a': 0, 'b': 1, 'c': 1}, {'a': 1, 'b': 0, 'c': 0}]
>>> mbn.reachability({'a': 0, 'b': 1, 'c': 1}, {'a': 1, 'b': 0, 'c': 0})
False
>>> mbn.reachability({'a': 0, 'b': 0, 'c': 0}, {'a': 1, 'b': 1, 'c': 1})
True
>>> list(mbn.attractors(reachable_from={'a': 0, 'b': 1, 'c': 0}))
[{'a': 0, 'b': 1, 'c': 1}]
"""

import os
import sys
from colomoto import minibn

from boolean import boolean
import clingo

from pyeda.boolalg import bdd
import pyeda.boolalg.expr
from pyeda.boolalg.expr import expr
sys.setrecursionlimit(max(100000, sys.getrecursionlimit()))

from biodivine_aeon import Bdd, BddVariableSet

__asplibdir__ = os.path.realpath(os.path.join(os.path.dirname(__file__), "asplib"))

clingo_options = ["-W", "no-atom-undefined"]
if hasattr(clingo, "version") and clingo.version() >= (5,5,0):
    clingo_options.append("--single-shot")

def aspf(basename):
    return os.path.join(__asplibdir__, basename)

def _clingo_domrec(mod, limit=0, project=False, extra_opts=[]):
    s = clingo.Control(clingo_options + extra_opts)
    s.configuration.solve.models = limit
    if project:
        s.configuration.solve.project = 1
    s.configuration.solve.enum_mode = "domRec"
    s.configuration.solver[0].heuristic = "Domain"
    s.configuration.solver[0].dom_mod = f"{mod},{16 if project else 0}"
    return s

def clingo_subsets(**opts):
    return _clingo_domrec(5, **opts)
def clingo_supsets(**opts):
    return _clingo_domrec(3, **opts)

def clingo_exists():
    s = clingo.Control(clingo_options)
    s.configuration.solve.models = 1
    return s

def clingo_enum(project=True, limit=0):
    s = clingo.Control(clingo_options)
    if project:
        s.configuration.solve.project = 1
    s.configuration.solve.models = limit
    return s

def s2v(s):
    return 1 if s > 0 else -1
def v2s(v):
    return 1 if v > 0 else 0

def ba_to_bdd(ba: boolean.BooleanAlgebra, f: boolean.Expression, ctx: BddVariableSet | None = None) -> Bdd:
    """
    Takes a `boolean.Expression` (with the associated `boolean.BooleanAlgebra`) and
    converts it to a `biodivine_aeon.Bdd`. 

    Note that the `Bdd` has an associated `biodivine_aeon.BddVariableSet` context, which maps the 
    variable IDs to names. You can provide your own context, 
    """
    ba_vars = f.symbols
    variables = sorted([ str(var) for var in ba_vars ])
    if ctx is None:        
        ctx = BddVariableSet(variables)
    else:
        # Check that all variables that exist in `f` also exist in `ctx`.
        assert all((ctx.find_variable(var) is not None) for var in variables)
    def ba_to_bdd_rec(f: boolean.Expression) -> Bdd:
        if type(f) is ba.TRUE or isinstance(f, minibn._TRUE):
            return ctx.mk_const(True)
        if type(f) is ba.FALSE or isinstance(f, minibn._FALSE):
            return ctx.mk_const(False)        
        if type(f) is ba.Symbol: 
            return ctx.mk_literal(str(f.obj), True)
        if type(f) is ba.NOT:
            assert len(f.args) == 1, "Cannot transform NOT with more than one argument."
            return ba_to_bdd_rec(f.args[0]).l_not()
        if type(f) is ba.AND:
            result = ctx.mk_const(True)
            for arg in f.args:
                result = result.l_and(ba_to_bdd_rec(arg))
            return result
        if type(f) is ba.OR:
            result = ctx.mk_const(False)
            for arg in f.args:
                result = result.l_or(ba_to_bdd_rec(arg))
            return result        
        raise NotImplementedError(str(f), type(f))
        
    return ba_to_bdd_rec(f)
        
def bdd_to_dnf(ba: boolean.BooleanAlgebra, f: Bdd) -> boolean.Expression:
    if f.is_true():
        return ba.TRUE
    if f.is_false():
        return ba.FALSE    
    ctx = f.__ctx__()
    # Technically, optimize=True is the default, but just in case.
    dnf = f.to_dnf(optimize=True)
    # Maps BDD variables to BooleanAlgebra Symbols.
    var_to_symbol = { var: ba.Symbol(ctx.get_variable_name(var)) for var in ctx.variable_ids() }
    ba_clauses = []
    for clause in dnf:
        literals = []
        for (var, value) in clause.items():            
            if value:
                literals.append(var_to_symbol[var])
            else:
                literals.append(ba.NOT(var_to_symbol[var]))                
        assert len(literals) > 0
        if len(literals) == 1:
            ba_clauses.append(literals[0])
        else:
            ba_clauses.append(ba.AND(*literals))
    assert len(ba_clauses) > 0 
    if len(ba_clauses) == 1:
        return ba_clauses[0]
    else:
        return ba.OR(*ba_clauses)

def is_unate(ba, f):
    pos_lits = set()
    neg_lits = set()
    def is_lit(f):
        if isinstance(f, ba.Symbol):
            pos_lits.add(f.obj)
            return True
        if isinstance(f, ba.NOT) \
                and isinstance(f.args[0], ba.Symbol):
            neg_lits.add(f.args[0].obj)
            return True
        return False

    def is_clause(f):
        if is_lit(f):
            return True
        if isinstance(f, ba.AND):
            for g in f.args:
                if not is_lit(g):
                    return False
            return True
        return False

    def test_monotonicity():
        both = pos_lits.intersection(neg_lits)
        return not both

    if f in [ba.TRUE, ba.FALSE]:
        return True
    if is_clause(f):
        return test_monotonicity()
    if isinstance(f, ba.OR):
        for g in f.args:
            if not is_clause(g):
                return False
        return test_monotonicity()
    return False

def asp_of_bdd(bid, b):
    _rules = dict()
    def register(node, nid=None):
        if node is bdd.BDDNODEONE:
            if nid is not None:
                _rules[bid] = f"bdd({clingo.String(nid)},1)"
            return 1
        elif node is bdd.BDDNODEZERO:
            if nid is not None:
                _rules[bid] = f"bdd({clingo.String(nid)},-1)"
            return -1
        nid = clingo.String(f"{bid}_n{id(node)}" if nid is None else nid)
        if nid not in _rules:
            var = clingo.String(bdd._VARS[node.root].qualname)
            lo = register(node.lo)
            hi = register(node.hi)
            a = f"bdd({nid},{var},{lo},{hi})"
            _rules[nid] = a
        return nid
    register(b.node, bid)
    return _rules.values()

def bddasp_of_boolfunc(f, i):
    e = expr(str(f).replace("!","~"))
    b = bdd.expr2bdd(e)
    atoms = asp_of_bdd(i, b)
    return "\n".join((f"{a}." for a in atoms))

def circuitasp_of_boolfunc(f, i, ba):
    atoms = []
    fid = clingo.String(i)
    def encode(expr):
        if expr == ba.TRUE:
            nodeid = "(constant,1)"
            atoms.append(f"circuit({nodeid}).")
        elif expr == ba.FALSE:
            nodeid = "(constant,-1)"
            atoms.append(f"circuit({nodeid}).")
        elif isinstance(expr, ba.Symbol):
            nodeid = f"(var,{clingo.String(expr.obj)})"
            atoms.append(f"circuit({fid},{nodeid}).")
        else:
            nodeid = f"n{id(expr)}"
            if isinstance(expr, ba.NOT):
                nodetype = "neg"
            elif isinstance(expr, ba.AND):
                nodetype = "and"
            elif isinstance(expr, ba.OR):
                nodetype = "or"
            else:
                raise NotImplementedError(type(expr))
            atoms.append(f"circuit({fid},{nodeid},{nodetype}).")
            for child in expr.args:
                cid = encode(child)
                atoms.append(f"circuitedge({fid},{nodeid},{cid}).")
        return nodeid
    root = encode(f)
    atoms.append(f"circuit({fid},root,{root}).\n")
    return "\n".join(atoms)


def expr2bpy(ex, ba):
    """
    converts a pyeda Boolean expression into a boolean.py one
    """
    if isinstance(ex, pyeda.boolalg.expr.Variable):
        return ba.Symbol(str(ex))
    elif isinstance(ex, pyeda.boolalg.expr._One):
        return ba.TRUE
    elif isinstance(ex, pyeda.boolalg.expr._Zero):
        return ba.FALSE
    elif isinstance(ex, pyeda.boolalg.expr.Complement):
        return ba.NOT(ba.Symbol(str(ex.__invert__())))
    elif isinstance(ex, pyeda.boolalg.expr.NotOp):
        return ba.NOT(expr2bpy(ex.x, ba))
    elif isinstance(ex, pyeda.boolalg.expr.OrOp):
        return ba.OR(*(expr2bpy(x, ba) for x in ex.xs))
    elif isinstance(ex, pyeda.boolalg.expr.AndOp):
        return ba.AND(*(expr2bpy(x, ba) for x in ex.xs))
    raise NotImplementedError(str(ex), type(ex))

DEFAULT_ENCODING = "mixed-dnf-bdd"

class MPBooleanNetwork(minibn.BooleanNetwork):
    """
    Most Permissive Boolean Network

    Extends ``colomoto.minibn.BooleanNetwork`` class by adding methods for
    computing reachable and attractor properties with the Most Permissive
    update mode.
    """
    supported_encodings = [
            "unate-dnf", "bdd", "circuit",
            "dnf-bdd", "mixed-dnf-bdd",
            "force-unate-dnf"]
    dnf_encodings = ["dnf-bdd", "unate-dnf", "force-unate-dnf",
                        "mixed-dnf-bdd"]
    nonpc_encodings = ["circuit"]

    def __init__(self, bn=minibn.BooleanNetwork(), auto_dnf=True,
                        simplify=False,
                        try_unate_hard=False,
                        encoding=DEFAULT_ENCODING):
        """
        Constructor for :py:class:`.MPBoooleanNetwork`.

        :param bn: Boolean network to copy from
        :type bn: :py:class:`colomoto.minibn.BooleanNetwork` or any type accepted by
            :py:class:`colomoto.minibn.BooleanNetwork` constructor
        :param bool auto_dnf: if ``False``, turns off automatic DNF
            transformation of local functions

        Examples:

        >>> mbn = MPBooleanNetwork("network.bnet")
        >>> bn = BooleanNetwork()
        >>> bn["a"] = ".."; ...
        >>> mbn = MPBooleanNetwork(bn)
        """
        assert encoding in self.supported_encodings
        self.auto_dnf = auto_dnf and encoding in self.dnf_encodings
        self.encoding = encoding
        self.try_unate_hard = try_unate_hard
        self._simplify = simplify
        self._is_unate = dict()
        super(MPBooleanNetwork, self).__init__(bn)

    def __setitem__(self, a, f):
        """
        Assigns the Boolean function ``f`` to component ``a``.
        Unless :py:attr:`.auto_dnf` is ``False``, ``f`` is converted into DNF
        form first.
        """
        if isinstance(f, str):
            f = self.ba.parse(f)
        f = self._autobool(f)
        if self.auto_dnf:
            original_f = f
            bdd = ba_to_bdd(self.ba, f)
            f = bdd_to_dnf(self.ba, bdd)
            
            # Run the old pipeline for comparison:
            e = expr(str(original_f).replace("!","~"))
            e = e.to_dnf()
            if self._simplify is not None:
                e = e.simplify()
            original_f = expr2bpy(e, self.ba)
            if self.try_unate_hard:
                original_f = minibn.simplify_dnf(self.ba, f)
            elif self._simplify:
                original_f = f.simplify()
            pyeda_dnf = 1
            if type(original_f) is self.ba.OR:
                pyeda_dnf = len(original_f.args)
            aeon_dnf = 1
            if type(f) is self.ba.OR:
                aeon_dnf = len(f.args)
            if pyeda_dnf < aeon_dnf:
                print(f"{pyeda_dnf}, {aeon_dnf}, PyEDA wins")
            if aeon_dnf < pyeda_dnf:
                print(f"{pyeda_dnf}, {aeon_dnf}, AEON wins")
            if pyeda_dnf == aeon_dnf:
                print(f"{pyeda_dnf}, {aeon_dnf}, draw")
        a = self._autokey(a)
        if self.encoding in self.dnf_encodings:
            self._is_unate[a] = is_unate(self.ba, f)
            if self.encoding == "unate-dnf":
                assert self._is_unate[a], f"'{f}' seems not unate. Try simplify()?"
        return super().__setitem__(a, f)

    def asp_of_bn(self, encoding=None):
        if encoding is None:
            encoding = self.encoding

        def clauses_of_dnf(f):
            if isinstance(f, boolean.OR):
                return f.args
            else:
                return [f]
        def literals_of_clause(c):
            def make_literal(l):
                if isinstance(l, boolean.NOT):
                    return (l.args[0].obj, -1)
                else:
                    return (l.obj, 1)
            lits = c.args if isinstance(c, boolean.AND) else [c]
            return map(make_literal, lits)
        def encode_dnf(f):
            facts = []
            for cid, c in enumerate(clauses_of_dnf(f)):
                for m, v in literals_of_clause(c):
                    facts.append(" clause(\"{}\",{},\"{}\",{}).".format(n, cid, m, v))
            return facts

        facts = []
        for n, f in self.items():
            facts.append("node(\"{}\").".format(n))
            if encoding in ["unate-dnf", "force-unate-dnf"]:
                f_encoding = "dnf"
            elif encoding == "dnf-bdd":
                f_encoding = "dnf" if self._is_unate[n] else "bdd"
            else:
                f_encoding = encoding
            if f == self.ba.FALSE:
                f = False
            elif f == self.ba.TRUE:
                f = True
            if isinstance(f, bool):
                facts.append("constant(\"{}\",{}).".format(n, s2v(f)))
            elif f_encoding == "dnf":
                facts.extend(encode_dnf(f))
            elif f_encoding == "bdd":
                facts.append(bddasp_of_boolfunc(f, n))
            elif f_encoding == "mixed-dnf-bdd":
                facts.extend(encode_dnf(f))
                if self._is_unate[n]:
                    facts.append(f"unate(\"{n}\").")
                else:
                    facts.append(bddasp_of_boolfunc(f, n))
            elif f_encoding == "circuit":
                facts.append(circuitasp_of_boolfunc(f, n, self.ba))
        return "".join(facts)

    def _file_eval(self):
        if self.encoding == "circuit":
            f = aspf("eval_circuit.asp")
        elif self.encoding == "mixed-dnf-bdd":
            f = aspf("eval_mixed.asp")
        else:
            f = aspf("mp_eval.asp")
        return f

    def rules_eval(self):
        f = self._file_eval()
        with open(f, "r") as fp:
            return fp.read()
    def load_eval(self, solver):
        f = self._file_eval()
        solver.load(f)

    def assert_pc_encoding(self):
        assert self.encoding not in self.nonpc_encodings, "Unsupported encoding"

    def asp_of_cfg(self, e, t, c):
        facts = ["timepoint({},{}).".format(e,t)]
        facts += [" mp_state({},{},\"{}\",{}).".format(e,t,n,s2v(s))
                    for (n,s) in c.items()]
        facts += [f"1 {{mp_state({e},{t},N,(-1;1))}} 1 :- node(N)."]
        #facts += [" 1 {{mp_state({},{},\"{}\",(-1;1))}} 1 :- node(N).".format(e,t,n)
        #                for n in self if n not in c]
        return "".join(facts)

    def reachability(self, x, y):
        """
        Returns ``True`` whenever the configuration `y` is reachable from `x`
        with the Most Permissive update mode.
        Configurations can be partially defined.
        In that case, returns ``True`` whenever there exists a configuration
        matching with `y` which is reachable with at least one configuration
        matching with `x`

        :param dict[str,int] x: initial configuration
        :param dict[str,int] y: target configuration
        """
        self.assert_pc_encoding()
        s = clingo_exists()
        self.load_eval(s)
        s.load(aspf("mp_positivereach-np.asp"))
        s.add("base", [], self.asp_of_bn())
        e = "default"
        t1 = 0
        t2 = 1
        s.add("base", [], self.asp_of_cfg(e,t1,x))
        s.add("base", [], self.asp_of_cfg(e,t2,y))
        s.add("base", [], "is_reachable({},{},{}).".format(e,t1,t2))
        s.ground([("base",[])])
        res = s.solve()
        return res.satisfiable

    def _ground_rules(self, ctl, rules):
        rules = "\n".join(rules)
        ctl.add("base", [], rules)
        ctl.ground([("base",[])])

    def _fixedpoints(self, reachable_from=None, constraints={}, limit=0):
        e = "fp"
        t2 = "fp"
        rules = [self.asp_of_cfg(e, t2, constraints)]
        rules.append(f"mp_reach({e},{t2},N,V) :- mp_state({e},{t2},N,V).")
        rules.append(f":- mp_state({e},{t2},N,V), mp_eval({e},{t2},N,-V).")
        rules.append(self.asp_of_bn())
        if reachable_from:
            self.assert_pc_encoding()
            t1 = "0"
            rules.append(open(aspf("mp_positivereach-np.asp")).read())
            rules.append(self.asp_of_cfg(e,t1,reachable_from))
            rules.append("is_reachable({},{},{}).".format(e,t1,t2))
        rules.append(f"#show. #show fixpoint(N,V) : mp_state({e},{t2},N,V).")
        rules.append(open(aspf("mp_eval.asp")).read())

        project = reachable_from and set(self.keys()).difference(reachable_from)
        s = clingo_enum(limit=limit, project=project)
        self._ground_rules(s, rules)
        return s

    def fixedpoints(self, reachable_from=None, constraints={}, limit=0):
        """
        Iterator over fixed points of the MPBN (i.e., of f)

        :param dict[str,int] reachable_from: restrict to the attractors
            reachable from the given configuration. Whenever partial, restrict
            attractors to the one reachable by at least one matching
            configuration.
        :param dict[str,int] constraints: consider only attractors matching with
            the given constraints.
        :param int limit: maximum number of solutions, ``0`` for unlimited.
        """
        s = self._fixedpoints(reachable_from=reachable_from,
                              constraints=constraints, limit=limit)
        for sol in s.solve(yield_=True):
            x = {n: None for n in self}
            data = sol.symbols(shown=True)
            for d in data:
                if d.name != "fixpoint":
                    continue
                (n, v) = d.arguments
                n = n.string
                v = v.number
                v = 1 if v == 1 else 0
                x[n] = v
            yield x

    def count_fixedpoints(self, reachable_from=None, constraints={}, limit=0):
        """
        Returns number of fixed points

        :param dict[str,int] reachable_from: restrict to the attractors
            reachable from the given configuration. Whenever partial, restrict
            attractors to the one reachable by at least one matching
            configuration.
        :param dict[str,int] constraints: consider only attractors matching with
            the given constraints.
        :param int limit: maximum number of solutions, ``0`` for unlimited.
        """
        s = self._fixedpoints(reachable_from=reachable_from,
                              constraints=constraints, limit=limit)
        return sum((1 for _ in s.solve(yield_=True)))


    def _trapspaces(self, reachable_from=None, subcube={}, limit=0,
                        mode="min", exclude_full=False):
        self.assert_pc_encoding()

        rules = []
        rules.append(self.asp_of_bn())
        rules.append(self.rules_eval())
        rules.append(open(aspf("mp_attractor.asp")).read())
        rules.append("#show attractor/2.")

        e = "__a"
        t2 = "final"
        if exclude_full and not subcube:
            rules.append(f"{{ mp_reach({e},{t2},N,(-1;1)): node(N) }} {len(self)*2-1}.")
        if reachable_from:
            t1 = "0"
            rules.append(open(aspf("mp_positivereach-np.asp")).read())
            rules.append(self.asp_of_cfg(e,t1,reachable_from))
            rules.append("is_reachable({},{},{}).".format(e,t1,t2))
            rules.append("mp_state({},{},N,V) :- attractor(N,V).".format(e,t2))

        for n, b in subcube.items():
            if isinstance(b, str):
                b = int(b)
            if b not in [0,1]:
                continue
            rules.append(":- mp_reach({},{},\"{}\",{}).".format(e,t2,n,s2v(1-b)))

        project = reachable_from and set(self.keys()).difference(reachable_from)
        solver = clingo_subsets if mode == "min" else clingo_supsets
        s = solver(limit=limit, project=project)
        self._ground_rules(s, rules)
        return s

    def _yield_trapspaces(self, *args, star="*", **kwargs):
        s = self._trapspaces(*args, **kwargs)
        for sol in s.solve(yield_=True):
            attractor = {n: None for n in self}
            data = sol.symbols(shown=True)
            for d in data:
                if d.name != "attractor":
                    continue
                (n, v) = d.arguments
                n = n.string
                v = v.number
                if v == 2:
                    v = star
                else:
                    v = 1 if v == 1 else 0
                if attractor[n] is not None:
                    if star is not None:
                        attractor[n] = star
                    else:
                        del attractor[n]
                else:
                    attractor[n] = v
            yield attractor

    def _count_trapspaces(self, *args, **kwargs):
        s = self._trapspaces(*args, **kwargs)
        return sum((1 for _ in s.solve(yield_=True)))

    def attractors(self, reachable_from=None, constraints={}, limit=0, star='*'):
        """
        Iterator over attractors of the MPBN (minimal trap spaces of the BN).
        An attractor is an hypercube, represented by a dictionnary mapping every
        component of the network to either ``0``, ``1``, or ``star``.

        :param dict[str,int] reachable_from: restrict to the attractors
            reachable from the given configuration. Whenever partial, restrict
            attractors to the one reachable by at least one matching
            configuration.
        :param dict[str,int] constraints: consider only attractors matching with
            the given constraints.
        :param int limit: maximum number of solutions, ``0`` for unlimited.
        :param str star: value to use for components which are free in the
            attractor
        """
        return self._yield_trapspaces(reachable_from=reachable_from,
                                subcube=constraints, limit=limit, star=star,
                                mode="min")
    minimal_trapspaces = attractors

    def maximal_trapspaces(self, limit=0, subcube={}, star="*",
                            exclude_full=True):
        return self._yield_trapspaces(subcube=subcube, limit=limit, star=star,
                                mode="max", exclude_full=exclude_full)

    def count_attractors(self, reachable_from=None, constraints={}, limit=0):
        """
        Returns number of attractors of the MPBN (minimal trap spaces of the BN).

        :param dict[str,int] reachable_from: restrict to the attractors
            reachable from the given configuration. Whenever partial, restrict
            attractors to the one reachable by at least one matching
            configuration.
        :param dict[str,int] constraints: consider only attractors matching with
            the given constraints.
        :param int limit: maximum number of solutions, ``0`` for unlimited.
        """
        return self._count_trapspaces(reachable_from=reachable_from,
                                subcube=constraints, limit=limit,
                                mode="min")
    count_minimal_trapspaces = count_attractors

    def count_maximal_trapspaces(self, reachable_from=None, constraints={}, limit=0):
        """
        Returns number of attractors of the MPBN (minimal trap spaces of the BN).

        :param dict[str,int] reachable_from: restrict to the attractors
            reachable from the given configuration. Whenever partial, restrict
            attractors to the one reachable by at least one matching
            configuration.
        :param dict[str,int] constraints: consider only attractors matching with
            the given constraints.
        :param int limit: maximum number of solutions, ``0`` for unlimited.
        """
        return self._count_trapspaces(reachable_from=reachable_from,
                                subcube=constraints, limit=limit,
                                mode="max")

    def has_cyclic_attractor(self):
        for a in self.attractors():
            if "*" in a.values():
                return True
        return False

    def reachable_from(self, x, reversed=False):
        """
        Returns an iterator over the configurations reachable from ``x`` with the
        Most Permissive update mode.
        Configuration ``x`` can be partially defined: in that case a configuration
        is yielded whnever it is reachable from at least one configuration
        matching with ``x``.

        Whenever ``reversed`` is ``True``, yields over the configurations that can
        reach `x` instead.
        """
        self.assert_pc_encoding()
        s = clingo_enum()
        self.load_eval(s)
        s.load(aspf("mp_positivereach-np.asp"))
        s.add("base", [], self.asp_of_bn())
        e = "default"
        t1 = 0
        t2 = 1
        s.add("base", [], self.asp_of_cfg(e,t1,x if not reversed else {}))
        s.add("base", [], self.asp_of_cfg(e,t2,{} if not reversed else x))
        s.add("base", [], "is_reachable({},{},{}).".format(e,t1,t2))
        t = t2 if not reversed else t1
        s.add("base", [], "#show." \
            f"#show mp_state(E,T,N,V) : mp_state(E,T,N,V), E={e}, T={t}.")
        s.ground([("base",[])])

        def cfg_of_asp(atoms):
            return {a.arguments[2].string: v2s(a.arguments[3].number) for a in atoms}
        for sol in s.solve(yield_=True):
            data = sol.symbols(shown=True)
            yield cfg_of_asp(data)

    def dynamics(self, update_mode="mp", **kwargs):
        """
        Returns a :py:class:`networkx.DiGraph` object representing the transitions between
        the configurations using the Most Permissive update mode by default.
        See :py:meth:`colomoto.minibn.BooleanNetwork.dynamics`.
        """
        if update_mode in ["mp", "most-permissive"]:
            update_mode = MostPermissiveDynamics
        return super().dynamics(update_mode=update_mode, **kwargs)


def load(filename, **opts):
    """
    Create a :py:class:`.MPBooleanNetwork` object from ``filename`` in BoolNet
    format; ``filename`` can be a local file or an URL.
    """
    return MPBooleanNetwork.load(filename, **opts)

class MostPermissiveDynamics(minibn.UpdateModeDynamics):
    def __init__(self, model, **opts):
        if not isinstance(model, MPBooleanNetwork)\
                and isinstance(model, minibn.BooleanNetwork):
            model = MPBooleanNetwork(model)
        super().__init__(model, **opts)

    def __call__(self, x):
        return self.model.reachable_from(x)

__all__ = ["load", "MPBooleanNetwork", "MostPermissiveDynamics"]
