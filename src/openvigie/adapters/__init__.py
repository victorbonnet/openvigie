"""Adapters: one plugin per agent type (FR-7).

Public interface: ``base`` (the ``Adapter`` contract, ``HostContext``, ``AdapterState``,
``ActivityBatch``), ``registry`` (discovery and safe ``run_*`` calls), and the helpers adapters
share: ``safeio`` reads, ``usage`` (incremental JSONL), ``spans`` (GenAI spans), ``caps``
(capabilities). ``testing`` holds the contract test suite.
"""
