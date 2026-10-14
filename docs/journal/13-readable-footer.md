# A more readable footer (issue #34)

The footer already had useful information. This change makes it easier to scan without changing its six rows, figures or fitting priorities.

## Styling is separate from content

Rich `Text` stores characters and style spans separately. A cyan label and a bold value still have the same plain text and cell width:

```python
return Text.assemble((f"{key}: ", "cyan"), (value, style))
```

That matters here: `fit` can still choose parts by importance, join them and truncate a long part without losing its colours. Model names, branches and tool arguments remain literal text, never markup.

The regression tests also exposed a subtle cause of the dullness: `Text(" · ", style="dim").join(parts)` gives the joined result a dim **base style**, so every value inherits dimming. `Text.assemble((" · ", "dim")).join(parts)` instead puts a span on just the separator's characters. The same fix applies between groups and between shortcut hints. Tests inspect effective styles, not just the list of spans, to catch inherited dimming.

Labels are no longer dim. Headline figures are bold, spending is magenta, and shortcut keys are bold cyan while their descriptions stay readable. Separators and incidental metadata remain quiet. The existing yellow permission warnings, red errors and green/yellow/red context meter keep their meaning; stopping is now a bold yellow warning too.

## A surface, not more rows

`FooterRows` uses Textual's `$surface` background and `$text` foreground. These follow the theme, giving the footer its own band without hard-coding a dark background. Borders or extra headings would consume space in an 80×24 terminal, so the height and padding stay unchanged.

Shortcut styling uses `partition(" ")`: it separates the first word (the key) from its action, returning three strings. A hint such as `/help`, with no space, still works: the separator and action are empty. The wording lives in one constant rather than separate plain and styled copies.

## Checks and things to try

- Tests check effective Rich styles after fitting, literal external text, warning colours and widths down to zero.
- Headless app tests check light and dark theme surfaces, six-row height, and resizing narrow and wide again.
- Run the fake-provider app and compare idle keys, a running tool, Escape stopping, and `/auto`.
- Try your terminal's light theme as well as its dark one: standard ANSI accent colours depend on the terminal palette, so automated style tests cannot guarantee subjective contrast on every palette.

No new settings or dependencies are needed. A broader, configurable colour palette can be added later if real terminal testing calls for it (D134).
