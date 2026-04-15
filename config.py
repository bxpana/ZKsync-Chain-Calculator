"""Shared configuration for ZKsync Chain Calculator scripts."""

import os
import sys
from dataclasses import dataclass


def load_env(path=".env"):
    """Read a .env file into os.environ. Handles KEY=VALUE lines, strips quotes,
    skips blank lines and comments."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key, value)


@dataclass
class ChainConfig:
    chain_name: str
    l1_rpc: str
    l2_rpc: str
    da_mode: str  # "rollup" or "validium"
    commit_operator: str
    prove_operator: str
    execute_operator: str
    watchdog_address: str
    pubdata_per_tx: int


def load_config(env_path=".env") -> ChainConfig:
    """Load configuration from .env file and environment variables."""
    load_env(env_path)

    required = ["L1_RPC", "L2_RPC", "COMMIT_OPERATOR", "PROVE_OPERATOR",
                 "EXECUTE_OPERATOR", "WATCHDOG_ADDRESS"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"Error: missing required .env variables: {', '.join(missing)}")
        print(f"Copy .env.example to .env and fill in the values.")
        sys.exit(1)

    da_mode = os.environ.get("DA_MODE", "rollup").lower()
    if da_mode not in ("rollup", "validium"):
        print(f"Error: DA_MODE must be 'rollup' or 'validium', got '{da_mode}'")
        sys.exit(1)

    return ChainConfig(
        chain_name=os.environ.get("CHAIN_NAME", "ZKsync Chain"),
        l1_rpc=os.environ["L1_RPC"],
        l2_rpc=os.environ["L2_RPC"],
        da_mode=da_mode,
        commit_operator=os.environ["COMMIT_OPERATOR"],
        prove_operator=os.environ["PROVE_OPERATOR"],
        execute_operator=os.environ["EXECUTE_OPERATOR"],
        watchdog_address=os.environ["WATCHDOG_ADDRESS"],
        pubdata_per_tx=int(os.environ.get("PUBDATA_PER_TX", "300")),
    )
