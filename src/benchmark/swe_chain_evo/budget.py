"""Retain the existing lifetime budget and add a bounded development budget."""
from pathlib import Path

from .common import existing_imports
from . import xml_transport

existing_imports()
from methods.evo_fifty.gateway import Broker as ExistingBroker
from methods.evo_fifty.spend import SpendLedger


class PairedLedger:
    def __init__(self, shared, development):
        self.shared = shared
        self.development = development

    def reserve(self, call_id, arm, phase, upper):
        self.shared.reserve(call_id, arm, phase, upper)
        try:
            self.development.reserve(call_id, arm, phase, upper)
        except BaseException:
            # No provider dispatch has happened. Retain a zero-cost cancelled
            # reservation in the shared ledger instead of erasing its identity.
            self.shared.settle(call_id, 0, 0, 0)
            raise

    def settle(self, call_id, cost, input_tokens, output_tokens):
        try:
            self.shared.settle(call_id, cost, input_tokens, output_tokens)
        finally:
            self.development.settle(call_id, cost, input_tokens, output_tokens)

    def uncertain(self, call_id):
        self.shared.uncertain(call_id)
        self.development.uncertain(call_id)

    def snapshot(self):
        shared = self.shared.snapshot()
        local = self.development.snapshot()
        shared["development"] = local
        shared["blocking"] = list({tuple(row) for row in shared["blocking"] + local["blocking"]})
        return shared


class Broker(ExistingBroker):
    def __init__(self, root, key, config, sender=None):
        super().__init__(root, key, config, sender=sender)
        local = SpendLedger(Path(root) / "development-spend.sqlite",
                            config["development_limit_usd"], allow_uncertain=True)
        self.ledger = PairedLedger(self.ledger, local)

    def execute(self, token, body):
        if self.config["runtime"].get("xml_editor_transport") == "json_envelope_v1":
            encoded, wrapped = xml_transport.encode(body)
            raw = super().execute(token, encoded)
            if wrapped:
                return xml_transport.decode(raw)
            return raw
        return super().execute(token, body)
