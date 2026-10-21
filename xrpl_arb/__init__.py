"""XRPL arbitrage scanner: finds XRP -> ... -> XRP cycles across the native order books and AMM pools.

Read-only. Nothing in this package holds a key or builds a transaction; it only asks a rippled
server for ledger state and reports what a cycle would return after fees.
"""
