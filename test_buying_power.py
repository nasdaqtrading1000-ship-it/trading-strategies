"""Offline tests: no credentials, real accounts, or network calls."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack, contextmanager
import sys

TEST_DIR = tempfile.TemporaryDirectory()
os.environ['REPLICATOR_DATA_DIR'] = TEST_DIR.name
spec = importlib.util.spec_from_file_location('replicator_app', Path(__file__).parent / 'replicator/replicator_app.py')
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class SizingTests(unittest.TestCase):
    def test_buying_power_not_equity_or_multiplier(self):
        self.assertEqual(app.dynamic_capital_per_operation({'capital_profile': '1_400'},
            {'equity': '10000', 'buying_power': '40000', 'multiplier': '4'}), (40, 40000, 1000))

    def test_available_amount_changes(self):
        self.assertEqual(app.dynamic_capital_per_operation({}, {'buying_power': '23000'})[0], 23)

    def test_round_down_to_cents(self):
        self.assertEqual(app.dynamic_capital_per_operation({}, {'buying_power': '40019.99'})[0], 40.01)

    def test_invalid_buying_power_never_falls_back_to_equity(self):
        for value in (None, '', 'bad', 'NaN', 'Infinity', '-Infinity', '0', '-5', '9.99'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                app.dynamic_capital_per_operation({}, {'buying_power': value, 'equity': '100000'})

    def test_old_config_migrates_without_changing_other_settings(self):
        cfg = {'capital_profile': '1_400', 'mode': 'alpaca_paper', 'auto_enabled': True,
               'selected_txt_names': ['Acumulacion.txt']}
        with patch.object(app, 'CONFIG_PATH', Path(TEST_DIR.name) / 'test-config.json'):
            app.CONFIG_PATH.write_text(json.dumps(cfg))
            loaded = app.load_config()
        self.assertEqual(loaded['capital_profile'], '1_1000')
        for key in ('mode', 'auto_enabled', 'selected_txt_names'):
            self.assertEqual(loaded[key], cfg[key])

    def run_scan(self, operations, accounts, mode='alpaca_paper', positions=None):
        broker = Mock()
        broker.account.side_effect = accounts
        broker.place_order.return_value = Mock(status='accepted', message='offline test')
        cfg = dict(app.DEFAULT_CONFIG, mode=mode, capital_profile='1_400',
                   capital_per_operation=9999, live_trading_confirmed=True)
        original_connection = app.state_connection
        @contextmanager
        def test_connection():
            conn = original_connection()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
        with ExitStack() as stack:
            stack.enter_context(patch.object(app, 'state_connection', test_connection))
            for name, value in {
                'load_config': cfg,
                'trading_window_status': {'is_open': True},
                'fetch_today_operations': operations,
                'broker_for_config': broker,
                'already_replicated': False,
                'accumulation_position_for_symbol': None,
                'opposite_strategy_position': None,
                'record_replication': None,
                'record_position_open': None,
                'record_position_close': None,
                'source_action_happened_today': True,
            }.items():
                stack.enter_context(patch.object(app, name, return_value=value))
            stack.enter_context(patch.object(app, 'operation_actions', side_effect=lambda op: [op['test_action']]))
            stack.enter_context(patch.object(app, 'open_position_row', side_effect=lambda conn, key: (positions or {}).get(key)))
            stack.enter_context(patch.object(app.urllib.request, 'urlopen', side_effect=AssertionError('Network forbidden')))
            result = app.scan_once()
        return result, broker

    @staticmethod
    def op(key, action='OPEN'):
        return dict(operation_key=key, symbol='TEST', test_action=action, entry_price=10,
                    txt_name='test.txt', strategy_name='Test', direction='LONG')

    def test_refresh_before_each_open(self):
        result, broker = self.run_scan([self.op('a'), self.op('b')],
                                     [{'buying_power': '40000'}, {'buying_power': '30000'}])
        self.assertEqual([c.args[2] for c in broker.place_order.call_args_list], [40, 30])
        self.assertEqual(broker.account.call_count, 2)
        self.assertEqual(result['account_buying_power'], 30000)
        self.assertNotIn('account_equity', result)

    def test_zero_buying_power_blocks_open_but_not_close(self):
        result, broker = self.run_scan([self.op('new'), self.op('held', 'CLOSE')],
                                     [{'buying_power': '0'}], positions={'held': {'shares': 7.5}})
        self.assertEqual(result['error_count'], 1)
        self.assertEqual(result['created_count'], 1)
        broker.place_order.assert_called_once()
        self.assertEqual(broker.place_order.call_args.args[1], 'CLOSE')
        self.assertEqual(broker.place_order.call_args.args[3], 7.5)

    def test_account_failure_does_not_use_cached_size(self):
        result, broker = self.run_scan([self.op('new')], [RuntimeError('offline account failure')])
        self.assertEqual(result['error_count'], 1)
        broker.place_order.assert_not_called()

    def test_live_validation_preserved_and_zero_bp_close_allowed(self):
        for blocked in (False, True):
            result, broker = self.run_scan([self.op('held', 'CLOSE')],
                [{'status': 'ACTIVE', 'trading_blocked': blocked, 'buying_power': '0'}],
                mode='alpaca_live', positions={'held': {'shares': 3}})
            self.assertEqual(result['created_count'], 0 if blocked else 1)
            self.assertEqual(broker.place_order.call_count, 0 if blocked else 1)

    def test_bad_price_never_uses_source_quantity(self):
        operation = dict(self.op('new'), entry_price=0, shares=500)
        result, broker = self.run_scan([operation], [{'buying_power': '40000'}])
        self.assertEqual(result['error_count'], 1)
        broker.place_order.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
