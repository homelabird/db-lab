import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('sim_under_test', Path(__file__).resolve().parents[1] / 'scripts/simulator.py')
sim = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sim)

class DatabaseError(Exception): pass

class Clock:
    def __init__(self): self.now=0.; self.sleeps=[]
    def monotonic(self): return self.now
    def sleep(self, delay):
        assert delay >= 0
        self.sleeps.append(delay); self.now += delay

class Connection:
    def __init__(self, clock, delay=.001, fail=False):
        self.clock=clock; self.delay=delay; self.fail=fail; self.starts=[]; self.rowcount=1
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def close(self): pass
    def fetchone(self): return (100,)
    def execute(self, sql, params=None):
        if sql.startswith('SELECT COALESCE'): return
        self.starts.append(self.clock.now); self.clock.now += self.delay
        if self.fail: raise DatabaseError('simulated timeout')

class PacingTests(unittest.TestCase):
    def execute(self, rate, delay=.001, fail=False, reconnect_fail=False):
        clock=Clock(); conn=Connection(clock,delay,fail)
        fake=types.SimpleNamespace(MySQLError=DatabaseError)
        def connect():
            if reconnect_fail and conn.starts:
                clock.now += .02
                raise DatabaseError('reconnect failed')
            return conn
        with patch.dict(sys.modules, {'pymysql':fake}), patch.object(sim,'connect',side_effect=connect), \
             patch.object(sim.time,'monotonic',clock.monotonic), patch.object(sim.time,'sleep',clock.sleep), \
             patch.object(sim.time,'time',side_effect=AssertionError('wall time used for pacing')), \
             contextlib.redirect_stdout(io.StringIO()):
            summary=sim.run(1,rate,42,'api')
        return summary,clock,conn
    def test_rates_1_10_100(self):
        for rate in (1,10,100):
            with self.subTest(rate=rate):
                s,c,db=self.execute(rate)
                self.assertEqual(s['attempted'],rate)
                self.assertAlmostEqual(s['elapsed_seconds'],1., places=6)
                self.assertGreater(s['sleep_seconds'],0)
                self.assertTrue(all(t<1 for t in db.starts))
                self.assertTrue(all(b-a>=1/rate-1e-9 for a,b in zip(db.starts,db.starts[1:])))
    def test_slow_db_drops_slots_instead_of_burst(self):
        s,c,db=self.execute(100,delay=.08)
        self.assertLessEqual(s['attempted'],13)
        self.assertGreater(s['operations']['skipped_schedule_slots'],0)
        self.assertTrue(all(b-a>=.08-1e-9 for a,b in zip(db.starts,db.starts[1:])))
    def test_failures_and_reconnect_are_separate(self):
        s,c,db=self.execute(10,fail=True)
        self.assertEqual(s['attempted'],10);self.assertEqual(s['failed'],10)
        self.assertEqual(s['succeeded'],0)
        self.assertEqual(s['successful_sql_latency_ms']['samples'],0)
        self.assertEqual(s['failed_attempt_latency_ms']['samples'],10)
        self.assertEqual(s['reconnect_latency_ms']['samples'],9)
    def test_reconnect_failure_does_not_abort_summary(self):
        s,c,db=self.execute(10,fail=True,reconnect_fail=True)
        self.assertEqual(s['failed'],s['attempted'])
        self.assertGreater(s['operations']['reconnect_failed'],0)
    def test_inflight_deadline_overrun_is_explicit(self):
        s,c,db=self.execute(10,delay=1.5)
        self.assertEqual(s['attempted'],1)
        self.assertAlmostEqual(s['deadline_overrun_seconds'],.5)
    def test_invalid_ranges(self):
        with patch.dict(sys.modules,{'pymysql':types.SimpleNamespace(MySQLError=DatabaseError)}):
            for seconds,rate in ((0,10),(1,0),(86401,10),(1,2001)):
                with self.assertRaises(ValueError):sim.run(seconds,rate,42,'api')
    def test_synthetic_and_measured_latency_labels(self):
        s,_,_=self.execute(10)
        self.assertIn('synthetic',s['stored_api_latency'])
        self.assertEqual(s['successful_sql_latency_ms']['p50'],1.)
        self.assertEqual(s['all_attempt_latency_ms']['samples'],s['attempted'])
