"""Isolated approval tests: fake RPC/DB gates and ephemeral unfunded test keys."""
import asyncio
from pathlib import Path
import tempfile
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from eth_abi import decode
from eth_account import Account
from smart_money import zeroex_approval as z
from smart_money.approval import APPROVAL_SPENDERS


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.policy = SimpleNamespace(run_mode='mainnet_live', follower_wallet=z.FOLLOWER,
            allowed_assets={z.USDG}, relationship_id='1', snapshot_hash='ab'*32,
            quote_policy=SimpleNamespace(max_gas_cost_wei='1000000000000000'))
        self.allowance = 0
        self.pending = 7
        self.chain = z.CHAIN_ID
        self.simulation = 1
        self.gas_price = 100
        self.balance = 10**18
        self.rpc = SimpleNamespace(call=AsyncMock(side_effect=self.call))

    async def call(self, method, params=None):
        if method == 'eth_chainId': return hex(self.chain)
        if method == 'eth_getCode': return '0x6001'
        if method == 'eth_getBalance': return hex(self.balance)
        if method == 'eth_gasPrice': return hex(self.gas_price)
        if method == 'eth_getTransactionCount': return hex(self.pending if params[1]=='pending' else 7)
        if method == 'eth_call':
            data = params[0]['data']
            if data.startswith('0x313ce567'):return '0x6'
            if data.startswith('0xdd62ed3e'):return hex(self.allowance)
            if data.startswith('0x095ea7b3'):return hex(self.simulation)
        raise AssertionError(method)

    async def test_exact_scope_and_existing_allowance(self):
        tx, result = await z.inspect(self.policy, self.rpc)
        self.assertFalse(result['private_key_read'])
        self.assertEqual(tx['value'], 0)
        self.assertEqual(tx['to'].lower(), z.USDG)
        spender, amount = decode(['address','uint256'], bytes.fromhex(tx['data'][10:]))
        self.assertEqual((spender,amount),(z.SPENDER,10000000))
        # This operator path once held the ONLY route to a 0x allowance. Copy
        # trading broke that: a sell needs an allowance for whichever token was
        # just bought, with no operator in the loop, so the executor now grants
        # 0x allowances through the bounded relationship path as well. This
        # script remains for a deliberate standing grant like the first one.
        self.assertIn(z.SPENDER, APPROVAL_SPENDERS)
        self.allowance=10000000
        tx,result=await z.inspect(self.policy,self.rpc)
        self.assertIsNone(tx)
        self.assertEqual(result['status'],'already_sufficient')

    async def test_wrong_chain_pending_partial_allowance_and_revert_rejected(self):
        for field, value in [('chain',1),('pending',8),('allowance',1),('simulation',0)]:
            old=getattr(self,field)
            setattr(self,field,value)
            with self.subTest(field=field),self.assertRaises(ValueError):
                await z.inspect(self.policy,self.rpc)
            setattr(self,field,old)

    async def test_gate_and_existing_journal_prevent_key_access(self):
        factory=Mock(side_effect=AssertionError('key accessed'))
        with tempfile.TemporaryDirectory() as tmp:
            journal=Path(tmp)/'attempt.jsonl'
            with patch.object(z,'require_mainnet_signing_enabled',side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    await z.execute(self.policy,self.rpc,None,journal=journal,signer_factory=factory)
            journal.touch()
            with patch.object(z,'require_mainnet_signing_enabled'):
                with self.assertRaises(FileExistsError):
                    await z.execute(self.policy,self.rpc,None,journal=journal,signer_factory=factory)
        factory.assert_not_called()

    async def test_mock_sign_and_broadcast_once_no_secret_in_journal(self):
        account=Account.create()
        self.policy.follower_wallet=account.address.lower()
        factory=Mock(return_value=SimpleNamespace(sign_transaction=lambda tx:
            bytes(account.sign_transaction(tx).raw_transaction)))
        broadcast=SimpleNamespace(broadcast=AsyncMock())
        with tempfile.TemporaryDirectory() as tmp, patch.object(z,'FOLLOWER',account.address.lower()), \
                patch.object(z,'require_mainnet_signing_enabled'), \
                patch.object(z,'confirm_relationship_token_approval',new=AsyncMock(return_value={'allowance_raw':'10000000'})):
            journal=Path(tmp)/'attempt.jsonl'
            result=await z.execute(self.policy,self.rpc,broadcast,journal=journal,signer_factory=factory)
            self.assertEqual(result['status'],'confirmed')
            self.assertEqual(broadcast.broadcast.await_count,1)
            saved=journal.read_text()
            self.assertNotIn(account.key.hex(),saved)
            self.assertNotIn(broadcast.broadcast.call_args.args[1].hex(),saved)
            with self.assertRaises(FileExistsError):
                await z.execute(self.policy,self.rpc,broadcast,journal=journal,signer_factory=factory)
            self.assertEqual(factory.call_count,1)

    async def test_monitor_lock_prevents_execute(self):
        args=SimpleNamespace(relationship='1',execute=True)
        with patch.object(z,'load_endpoint_env'),patch.object(z,'load_enabled_relationship_policy',return_value=self.policy), \
                patch.dict(z.os.environ,{'ROBINHOOD_RPC_URL':'https://example.invalid'}), \
                patch.object(z,'runtime_instance_lock',side_effect=RuntimeError('locked')), \
                patch.object(z,'execute',new_callable=AsyncMock) as execute:
            with self.assertRaises(RuntimeError):await z.run(args)
            execute.assert_not_called()

    async def test_default_check_never_invokes_signing(self):
        args=SimpleNamespace(relationship='1',execute=False)
        with patch.object(z,'load_endpoint_env'),patch.object(z,'load_enabled_relationship_policy',return_value=self.policy), \
                patch.dict(z.os.environ,{'ROBINHOOD_RPC_URL':'https://example.invalid'}), \
                patch.object(z,'ReadOnlyRpc',return_value=self.rpc),patch.object(z,'LiveDatabaseSigner') as signer:
            result=await z.run(args)
            self.assertEqual(result['status'],'ready')
            signer.assert_not_called()

    async def test_original_fee_cap_survives_normal_price_changes(self):
        tx,_ = await z.inspect(self.policy,self.rpc)
        original = dict(tx)
        for price in (99,101,119,120):
            self.gas_price=price
            current,evidence=await z.inspect(self.policy,self.rpc,fixed_transaction=tx)
            self.assertEqual(current,original)
            self.assertEqual(evidence['maximum_gas_cost_wei'],'12000000')
        self.assertEqual(tx,original)

    async def test_upgrade_sets_total_ten_not_additional_ten(self):
        self.allowance=100000
        tx,evidence=await z.inspect(self.policy,self.rpc)
        self.assertEqual(evidence['allowance_raw'],'100000')
        spender,amount=decode(['address','uint256'],bytes.fromhex(tx['data'][10:]))
        self.assertEqual((spender,amount),(z.SPENDER,10000000))
        self.assertEqual(evidence['amount_raw'],'10000000')
        self.assertIn('10000000',str(z.JOURNAL))

    async def test_original_cap_still_enforces_gas_balance_and_policy(self):
        tx,_=await z.inspect(self.policy,self.rpc)
        self.gas_price=121
        with self.assertRaisesRegex(ValueError,'below current gas'):
            await z.inspect(self.policy,self.rpc,fixed_transaction=tx)
        self.gas_price=90
        self.balance=11999999
        with self.assertRaisesRegex(ValueError,'insufficient gas balance'):
            await z.inspect(self.policy,self.rpc,fixed_transaction=tx)
        self.balance=10**18
        self.policy.quote_policy.max_gas_cost_wei='11999999'
        with self.assertRaisesRegex(ValueError,'gas exceeds limit'):
            await z.inspect(self.policy,self.rpc,fixed_transaction=tx)

    async def test_fixed_transaction_rejects_nonce_and_payload_changes(self):
        tx,_=await z.inspect(self.policy,self.rpc)
        for field,value in [('nonce',8),('to',z.SPENDER),('data','0x'),('value',1),
                ('gas',200000),('chainId',1),('maxPriorityFeePerGas',1)]:
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'identity or nonce'):
                await z.inspect(self.policy,self.rpc,fixed_transaction={**tx,field:value})

    async def test_signing_pipeline_with_fluctuating_gas_keeps_signed_fee(self):
        account=Account.create()
        self.policy.follower_wallet=account.address.lower()
        prices=iter((100,99,101))
        async def changing_rpc(method,params=None):
            if method=='eth_gasPrice':return hex(next(prices))
            return await self.call(method,params)
        self.rpc.call.side_effect=changing_rpc
        signed=[]
        def sign(tx):
            signed.append(dict(tx))
            return bytes(account.sign_transaction(tx).raw_transaction)
        broadcaster=SimpleNamespace(broadcast=AsyncMock())
        with tempfile.TemporaryDirectory() as tmp,patch.object(z,'FOLLOWER',account.address.lower()), \
                patch.object(z,'require_mainnet_signing_enabled'), \
                patch.object(z,'confirm_relationship_token_approval',new=AsyncMock(return_value={})):
            result=await z.execute(self.policy,self.rpc,broadcaster,
                journal=Path(tmp)/'journal',signer_factory=Mock(return_value=SimpleNamespace(sign_transaction=sign)))
        self.assertEqual(result['status'],'confirmed')
        self.assertEqual(signed[0]['maxFeePerGas'],120)
        self.assertEqual(len(signed),1)
        broadcaster.broadcast.assert_awaited_once()


class ExecutionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE execution_plans(plan_id TEXT, follower_wallet TEXT, status TEXT)')
        self.db.execute('CREATE TABLE execution_attempts(plan_id TEXT, status TEXT)')

    def check(self, status, attempts):
        self.db.execute('DELETE FROM execution_plans')
        self.db.execute('DELETE FROM execution_attempts')
        self.db.execute('INSERT INTO execution_plans VALUES(?,?,?)', ('p',z.FOLLOWER,status))
        self.db.executemany('INSERT INTO execution_attempts VALUES(?,?)', [('p',s) for s in attempts])
        return self.db.execute(z.UNRESOLVED_PLANS_SQL.replace('%s','?'), (z.FOLLOWER,)).fetchone()[0]

    def test_signed_plans_with_final_receipts_and_cancelled_without_attempts_pass(self):
        for status, attempts in [('signed',['confirmed']),('signed',['reverted']),
                ('signed',['replaced','confirmed']),('cancelled',[])]:
            with self.subTest(status=status,attempts=attempts):
                self.assertEqual(self.check(status,attempts),0)

    def test_pending_missing_ambiguous_unknown_and_orphaned_remain_blocked(self):
        for status, attempts in [('prepared',[]),('signed',[]),('signed',['signed']),
                ('signed',['observed_pending']),('signed',['orphaned']),('signed',['replaced']),
                ('signed',['confirmed','observed_pending']),('signed',['confirmed','reverted']),
                ('signed',['confirmed','unknown']),('signed',[None]),('cancelled',['signed']),
                ('cancelled',['confirmed']),('unknown',[]),(None,[])]:
            with self.subTest(status=status,attempts=attempts):
                self.assertEqual(self.check(status,attempts),1)

    def test_other_wallet_is_not_in_scope(self):
        self.db.execute('INSERT INTO execution_plans VALUES(?,?,?)',('other','0x'+'55'*20,'prepared'))
        self.assertEqual(self.db.execute(z.UNRESOLVED_PLANS_SQL.replace('%s','?'),(z.FOLLOWER,)).fetchone()[0],0)
