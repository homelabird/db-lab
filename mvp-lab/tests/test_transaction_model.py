"""Independent snapshot corruption checks: accounting totals alone are insufficient."""
import copy
import json
from unittest import TestCase
from mvp_app.transaction_model import Purchase, audit, canonical, digest, options, plan, SCENARIOS, TABLES


def good():
    oid = 'a'*32
    return {
        'inventory': [{'case_id': 'case', 'initial_qty': 10, 'available': 8, 'unit_price': 123}],
        'wallets': [{'case_id': 'case', 'owner': 'buyer', 'initial_balance': 10000, 'balance': 9754},
                    {'case_id': 'case', 'owner': 'merchant', 'initial_balance': 0, 'balance': 246}],
        'requests': [{'case_id':'case','request_key':'key','fingerprint':Purchase('case','key',2).fingerprint,'order_id':oid}],
        'orders': [{'id':oid,'case_id':'case','request_key':'key','quantity':2,'amount':246,'state':'paid'}],
        'ledger': [{'id':'b'*32,'case_id':'case','order_id':oid,'owner':'buyer','phase':'paid','delta':-246},
                   {'id':'c'*32,'case_id':'case','order_id':oid,'owner':'merchant','phase':'paid','delta':246}],
        'events': [{'id':'d'*32,'case_id':'case','order_id':oid,'kind':'paid','payload':json.dumps({'order_id':oid,'quantity':2,'amount':246,'state':'paid'})}],
    }


class OracleTests(TestCase):
    def bad(self, snapshot, name):
        result = audit(snapshot)
        self.assertFalse(result['passed'])
        self.assertIn(name, [v['check'] for v in result['violations']], result)

    def test_valid_rows_and_does_not_mutate_input(self):
        value = good(); before = copy.deepcopy(value)
        self.assertTrue(audit(value)['passed'])
        self.assertEqual(value, before)

    def test_remaining_stock_zero_alone_does_not_prove_no_oversell(self):
        value = good(); value['inventory'][0]['initial_qty'] = 1; value['inventory'][0]['available'] = 0
        self.bad(value, 'stock_conservation')

    def test_stock_negative(self):
        value = good(); value['inventory'][0]['available'] = -1
        self.bad(value, 'stock_bounds')

    def test_stock_above_initial(self):
        value = good(); value['inventory'][0]['available'] = 11
        self.bad(value, 'stock_bounds')

    def test_wallet_total_can_balance_while_transfers_are_wrong(self):
        value = good(); value['wallets'][0]['balance'] -= 1; value['wallets'][1]['balance'] += 1
        self.bad(value, 'wallet_ledger_agreement')
        self.assertNotIn('money_conservation', [v['check'] for v in audit(value)['violations']])

    def test_money_creation(self):
        value = good(); value['wallets'][0]['balance'] += 1
        self.bad(value, 'money_conservation')

    def test_negative_wallet(self):
        value = good(); value['wallets'][1]['balance'] = -1
        self.bad(value, 'nonnegative_balance')

    def test_wrong_price(self):
        value = good(); value['orders'][0]['amount'] += 1
        self.bad(value, 'order_values')

    def test_wrong_order_state(self):
        value = good(); value['orders'][0]['state'] = 'shipping'
        self.bad(value, 'order_values')

    def test_duplicate_logical_request(self):
        value = good(); value['orders'].append({**value['orders'][0], 'id': 'e'*32})
        self.bad(value, 'one_order_per_request')

    def test_missing_claim(self):
        value = good(); value['requests'] = []
        self.bad(value, 'claim_payload_and_order')
        self.assertTrue(audit(value, require_claims=False)['passed'])

    def test_wrong_fingerprint(self):
        value = good(); value['requests'][0]['fingerprint'] = '0'*64
        self.bad(value, 'claim_payload_and_order')

    def test_claim_without_order(self):
        value = good(); value['requests'][0]['order_id'] = '0'*32
        self.bad(value, 'claims_no_orphans')

    def test_extra_request_alias_cannot_point_to_same_order(self):
        value = good(); value['requests'].append({**value['requests'][0], 'request_key':'other-key'})
        self.bad(value, 'claim_targets_unique')
        self.bad(value, 'claim_request_identity')

    def test_ledger_orphan(self):
        value = good(); value['ledger'][0]['order_id'] = '0'*32
        self.bad(value, 'ledger_no_orphans')

    def test_balanced_duplicate_ledger_entries_are_not_safe(self):
        value = good(); value['ledger'] += copy.deepcopy(value['ledger'])
        self.bad(value, 'order_ledger_exact')
        self.assertNotIn('ledger_zero_sum', [v['check'] for v in audit(value)['violations']])

    def test_missing_one_ledger_side(self):
        value = good(); value['ledger'].pop()
        self.bad(value, 'ledger_zero_sum')

    def test_unknown_ledger_owner(self):
        value = good(); value['ledger'][0]['owner'] = 'someone-else'
        self.bad(value, 'known_ledger_owners')

    def test_outbox_orphan(self):
        value = good(); value['events'][0]['order_id'] = '0'*32
        self.bad(value, 'events_no_orphans')

    def test_outbox_missing(self):
        value = good(); value['events'] = []
        self.bad(value, 'order_events_exact')

    def test_outbox_duplicate(self):
        value = good(); value['events'] *= 2
        self.bad(value, 'order_events_exact')

    def test_outbox_content_not_just_count(self):
        value = good(); value['events'][0]['payload'] = '{}'
        self.bad(value, 'event_payload_matches')

    def test_outbox_invalid_json_not_success(self):
        value = good(); value['events'][0]['payload'] = 'broken'
        self.bad(value, 'event_payload_matches')

    def test_mixed_cases_rejected(self):
        value = good(); value['events'][0]['case_id'] = 'other'
        with self.assertRaises(ValueError): audit(value)

    def test_missing_table_not_empty_success(self):
        value = good(); del value['ledger']
        with self.assertRaises(ValueError): audit(value)

    def test_empty_or_overbudget_snapshot_rejected(self):
        for value in ({t:[] for t in TABLES}, {**good(),'events':[{}]*1001}):
            with self.assertRaises(ValueError): audit(value)

    def test_no_extra_wallets(self):
        value = good(); value['wallets'].append(value['wallets'][0])
        with self.assertRaises(ValueError): audit(value)

    def test_negative_price_or_noninteger_values_fail(self):
        value = good(); value['inventory'][0]['unit_price'] = -1
        self.bad(value, 'fixture_values')
        value = good(); value['wallets'][0]['initial_balance'] = 10000.0
        self.bad(value, 'wallet_value_types')


class ModelTests(TestCase):
    def test_six_scenarios_and_repeatable_plan(self):
        self.assertEqual(len(SCENARIOS), 6)
        for scenario in SCENARIOS:
            self.assertEqual(plan(scenario, 8, 23), plan(scenario, 8, 23))
            self.assertFalse(plan(scenario)['source_writes'])
            self.assertFalse(plan(scenario)['kafka_delivery'])

    def test_options_limits_and_boolean_rejected(self):
        for clients in (0, 1, 9, True, 2.0):
            with self.assertRaises(ValueError): options('stock-race', clients, 0)
        for seed in (-1, 2**31, True, 1.0):
            with self.assertRaises(ValueError): options('stock-race', 4, seed)
        with self.assertRaises(ValueError): options('unknown', 4, 0)

    def test_payload_fingerprint_distinguishes_quantity_case_and_key(self):
        base = Purchase('case','key',1)
        values = {base.fingerprint, Purchase('case','key',2).fingerprint,
                  Purchase('other','key',1).fingerprint, Purchase('case','other',1).fingerprint}
        self.assertEqual(len(values), 4)
        self.assertEqual(base.fingerprint, Purchase('case','key',1).fingerprint)

    def test_purchase_bounds_and_sql_input(self):
        for values in (('case','key',True),('case','key',0),('case','key',21),('case','key',1.0),
                       ('../path','k',1),('case',"';DELETE",1),('case','한글',1),('case','k'*65,1)):
            with self.assertRaises(ValueError): Purchase(*values)

    def test_canonical_hash_ignores_dict_order_not_values(self):
        self.assertEqual(digest({'a':1,'b':2}), digest({'b':2,'a':1}))
        self.assertNotEqual(digest({'a':1}), digest({'a':2}))
