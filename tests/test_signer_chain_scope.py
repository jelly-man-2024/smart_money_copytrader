"""The signer accepts a configured chain and refuses an unknown one."""
import unittest

from smart_money import registry as R
from smart_money.key_source import _validate_transaction


def transaction(chain_id):
    return {"chainId": chain_id, "nonce": 0, "to": "0x" + "11" * 20, "value": 0,
            "data": "0x095ea7b3", "gas": 80_000, "maxFeePerGas": 2 * 10 ** 10,
            "maxPriorityFeePerGas": 10 ** 9, "type": 2}


class SignerChainScopeTests(unittest.TestCase):
    def test_every_configured_chain_is_signable(self):
        # Pinned to one chain, a correctly configured second chain could not be
        # signed at all, which is what blocked the Arc approval.
        for chain_id in R.CHAINS:
            with self.subTest(chain_id=chain_id):
                _validate_transaction(transaction(chain_id))

    def test_an_unknown_chain_is_refused(self):
        for chain_id in (999999, 1, 0, None, "4663"):
            with self.subTest(chain_id=chain_id):
                with self.assertRaisesRegex(ValueError, "chain mismatch"):
                    _validate_transaction(transaction(chain_id))

    def test_the_other_transaction_checks_still_hold(self):
        for field, value in (("type", 0), ("nonce", -1), ("gas", "80000"),
                             ("data", "095ea7b3"), ("value", None)):
            with self.subTest(field=field):
                payload = transaction(R.ARC.chain_id)
                payload[field] = value
                with self.assertRaises(ValueError):
                    _validate_transaction(payload)
        extra = transaction(R.ARC.chain_id)
        extra["gasPrice"] = 1
        with self.assertRaisesRegex(ValueError, "invalid signing transaction fields"):
            _validate_transaction(extra)


if __name__ == "__main__":
    unittest.main()
