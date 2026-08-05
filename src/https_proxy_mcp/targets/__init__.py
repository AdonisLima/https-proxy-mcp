"""Capture Target implementations.

Each target arranges routing and trust for one kind of thing under test, and can
diagnose why capture is not working. v1 ships an iOS Simulator target and a
trivial local target used to exercise the core without any mobile tooling.
"""
