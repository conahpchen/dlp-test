"""Control-plane admission, daily reservations and bounded response cache."""

import hashlib
import json
import logging
import os
import sqlite3
from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import monotonic, time


IN_FLIGHT = BoundedSemaphore(2)


class InferenceUnavailable(RuntimeError):
    def __init__(self, message, code='model_unavailable'):
        self.code = code
        super().__init__(message)


def budget_path():
    configured = os.environ.get("RESEARCH_MODEL_BUDGET_FILE")
    if configured:
        return Path(configured)
    research = Path(os.environ.get("RESEARCH_STATE_FILE", "var/research.sqlite"))
    return research.with_name("model-budget.sqlite")


def usage_today():
    ledger = budget_path()
    if not ledger.is_file():
        return None
    with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_budget'").fetchone():
            return None
        row = db.execute("SELECT tokens,microusd FROM daily_budget WHERE day=?",
                         (int(time()) // 86400,)).fetchone()
        return {"reserved_tokens": row[0], "estimated_cost_microusd": row[1]} if row else None


class ModelBudget:
    def __init__(self, ledger, daily_tokens, daily_cost_microusd=0,
                 input_rate=0, output_rate=0, *, cache_ttl=60, cooldown=30, failures=3):
        self.ledger = Path(ledger)
        self.daily_tokens = daily_tokens
        self.daily_cost = daily_cost_microusd
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.cache_ttl = cache_ttl
        self.cooldown = cooldown
        self.failure_limit = failures
        self.lock = Lock()
        self.cache = OrderedDict()
        self.in_flight = set()
        self.failures = 0
        self.open_until = 0.0
        self.generation = 0

    @classmethod
    def configured(cls, ledger):
        def config(name, default):
            value = int(os.environ.get(name, default))
            if not 0 <= value <= 10**12:
                raise ValueError(f"Invalid {name}")
            return value

        daily_tokens = config("RESEARCH_MODEL_DAILY_TOKENS", 1_000_000)
        daily_cost = config("RESEARCH_MODEL_DAILY_COST_MICROUSD", 0)
        input_rate = config("RESEARCH_MODEL_INPUT_MICROUSD_PER_MILLION", 0)
        output_rate = config("RESEARCH_MODEL_OUTPUT_MICROUSD_PER_MILLION", 0)
        if daily_cost and (not input_rate or not output_rate):
            raise ValueError("A cost budget requires both model token rates")
        return cls(ledger, daily_tokens, daily_cost, input_rate, output_rate)

    def _cost(self, input_tokens, output_tokens):
        return (input_tokens * self.input_rate + output_tokens * self.output_rate + 999_999) // 1_000_000

    def reserve(self, input_tokens, output_tokens):
        reserved = input_tokens + output_tokens
        priced = self._cost(input_tokens, output_tokens)
        day = int(time()) // 86400
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.ledger, timeout=2)) as db, db:
            self.ledger.chmod(0o600)
            db.execute("CREATE TABLE IF NOT EXISTS daily_budget ("
                       "day INTEGER PRIMARY KEY, tokens INTEGER NOT NULL, microusd INTEGER NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT tokens,microusd FROM daily_budget WHERE day=?", (day,)).fetchone()
            tokens, cost = row if row is not None else (0, 0)
            if tokens + reserved > self.daily_tokens:
                raise InferenceUnavailable("Model daily token budget exhausted", "model_budget_tokens")
            if self.daily_cost and cost + priced > self.daily_cost:
                raise InferenceUnavailable("Model daily cost budget exhausted", "model_budget_cost")
            db.execute("INSERT OR REPLACE INTO daily_budget VALUES (?,?,?)",
                       (day, tokens + reserved, cost + priced))

        return day, reserved, priced

    def _reconcile(self, reservation, usage):
        if not isinstance(usage, dict):
            return
        inputs, outputs = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if any(type(value) is not int or not 0 <= value <= 10**9 for value in (inputs, outputs)):
            return
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if type(reasoning) is int and not 0 <= reasoning <= outputs:
            # An inconsistent report cannot safely release the reserved budget.
            return
        day, reserved, estimated = reservation
        try:
            with closing(sqlite3.connect(self.ledger, timeout=2)) as db, db:
                db.execute("UPDATE daily_budget SET tokens=tokens+?,microusd=microusd+? WHERE day=?",
                           (inputs + outputs - reserved, self._cost(inputs, outputs) - estimated, day))
        except sqlite3.Error:
            logging.getLogger(__name__).warning("Model budget reconciliation could not be recorded")

    def choose(self, payload, input_tokens, send, *, counts_as_failure):
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).digest()
        with self.lock:
            now = monotonic()
            cached = self.cache.get(key)
            if cached and cached[0] > now:
                self.cache.move_to_end(key)
                return deepcopy(cached[1])
            if cached:
                del self.cache[key]
            if self.open_until > now or (self.open_until and self.in_flight):
                raise InferenceUnavailable("Model circuit is cooling down", "model_circuit_open")
            if key in self.in_flight:
                raise InferenceUnavailable("Identical model request already in flight", "model_concurrency")
            if not IN_FLIGHT.acquire(blocking=False):
                raise InferenceUnavailable("Model concurrency budget exhausted", "model_concurrency")
            generation = self.generation
            self.in_flight.add(key)
        try:
            reservation = self.reserve(input_tokens, payload["max_tokens"])
            try:
                result = send()
            except Exception as error:
                self._reconcile(reservation, getattr(error, "usage", None))
                with self.lock:
                    if generation == self.generation and counts_as_failure(error):
                        self.failures += 1
                        if self.failures >= self.failure_limit:
                            self.open_until = monotonic() + self.cooldown
                            self.generation += 1
                raise
            self._reconcile(reservation, result[1].get("usage"))
            with self.lock:
                if generation == self.generation:
                    self.failures = 0
                    self.open_until = 0.0
                self.cache[key] = (monotonic() + self.cache_ttl, deepcopy(result))
                while len(self.cache) > 32:
                    self.cache.popitem(last=False)
            return result
        finally:
            with self.lock:
                self.in_flight.remove(key)
                IN_FLIGHT.release()
