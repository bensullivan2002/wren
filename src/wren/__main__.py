"""Lets you run Wren with `python -m wren`, without installing the `wren` command."""

from wren.cli import main

raise SystemExit(main())
