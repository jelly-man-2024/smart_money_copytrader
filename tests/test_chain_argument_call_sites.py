"""Every call that needs a chain passes one, including rarely-executed paths.

Making the chain a required argument is only half the guard: a call site that
never runs in the test suite still type-checks at import time and fails in
production instead. A live Arc sell failed exactly that way — three call sites
inside propose_sell were missed, and only a real sell reached them. This walks
the package's own syntax tree so a missed call site fails here, not on chain.
"""
import ast
import pathlib
import unittest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "src" / "smart_money"

# function name -> how many positional arguments it takes including the chain
CHAIN_ARGUMENT_CALLS = {
    "aggregator_route_definition": 4,
    "budget_bucket": 2,
}


def call_sites(name):
    for source in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                yield source.name, node


class ChainArgumentCallSiteTests(unittest.TestCase):
    def test_every_call_site_passes_a_chain(self):
        for name, expected in CHAIN_ARGUMENT_CALLS.items():
            seen = 0
            for filename, node in call_sites(name):
                if isinstance(node.func, ast.Name) and node.func.id != name:
                    continue
                seen += 1
                passed = len(node.args) + len(node.keywords)
                with self.subTest(call=name, file=filename, line=node.lineno):
                    self.assertGreaterEqual(
                        passed, expected,
                        f"{filename}:{node.lineno} calls {name}() with {passed} "
                        f"arguments; it needs {expected}, including the chain")
            with self.subTest(call=name):
                self.assertTrue(seen, f"no call site found for {name}; update this test")


if __name__ == "__main__":
    unittest.main()
