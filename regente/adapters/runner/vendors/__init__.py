# -*- coding: utf-8 -*-
"""One module per coding-agent vendor. They never import each other.

The rule is structural rather than a convention, because a convention is a
comment and this needs to be a wall: `test_boundaries.py` walks this package
with the AST and fails if any module here imports another module here.

It exists because the second profile written in this project imported a helper
from the first. Nothing broke, every test passed, and the property the whole
milestone claims -- that swapping the agent is one file -- had quietly stopped
being true: deleting vendor one would have broken vendor two.

Shared work belongs one level up, in `cli_agent.py` (the execution contract) or
`mission_text.py` (rendering a mission for an agent that reads text). Anything a
second vendor would want is by definition not vendor-specific.
"""
