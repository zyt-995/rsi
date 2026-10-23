"""Use the existing lifetime ledger; all development checks share one $5 cap."""
from pathlib import Path

from swe_chain_evo.budget import Broker as SWEBroker, PairedLedger
from methods.evo_fifty.gateway import Broker as ProviderBroker
from methods.evo_fifty.spend import SpendLedger


class Broker(SWEBroker):
    def __init__(self, root, key, config, sender=None):
        ProviderBroker.__init__(self, root, key, config, sender=sender)
        if config.get("development_limit_usd"):
            path = config.get("development_ledger_path", Path(root) / "development-spend.sqlite")
            local = SpendLedger(Path(path), config["development_limit_usd"], allow_uncertain=True)
            self.ledger = PairedLedger(self.ledger, local)
