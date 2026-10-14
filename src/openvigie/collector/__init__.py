"""The collector: one daemon per host that runs the adapters and pushes to the hub.

Public interface: ``daemon.Collector`` (the loop), ``client.HubClient`` (HTTP to the hub),
``enrol.enrol``, ``host.build_context``, ``scan`` (``openvigie scan``), ``otlp.OtlpReceiver``.
"""
