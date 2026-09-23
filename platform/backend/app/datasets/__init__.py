"""Rolling replay-dataset collection.

`victorialogs` queries the gateway's log store, `builder` turns a time window of
matched production traffic into a published replay dataset, and `service` is the
always-on pod that runs builds on a schedule (and on demand).

The on-disk layout, the pointer file, and the conversion rules deliberately live
in `bench/replay_test/` instead — `bench/` is a pure library shared with the
standalone CLI and the worker, so there is exactly one implementation of "what a
replay dataset is" and "which build is current".
"""
