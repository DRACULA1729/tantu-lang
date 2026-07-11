"""Tantu — a statically-typed, Python-readable language with fibers + channels.

Compiled to bytecode and run on a hand-built stack VM. See DESIGN.md (v1.2) at the
repo root for the full language & toolchain contract.

Pipeline: source -> lexer -> parser -> checker -> compiler -> VM.
"""

__version__ = "0.1.0"
