# Screenshots

Everything here comes from the demo (`python tools/demo.py`): made-up hubs, a fake tailnet domain and
fake addresses. Nothing from a real tailnet.

To regenerate them, with the demo running:

```sh
CHROME="/path/to/chrome" node tools/screenshots.mjs docs/screenshots
python tools/screenshots_gif.py docs/screenshots
```

| File | What |
|---|---|
| `demo.gif` | the live mesh map, the command palette, Control |
| `index.png` | a hub's index (Acid theme) |
| `control.png` | Control (Abyss theme) |
| `network-paper.png` | the hub mesh in Control (Paper theme) |
| `phone.png` | the index on a phone |

Before adding new ones: no real hub names, domains, IPs, clipboard contents, terminal output, tokens
or file paths, and no browser profile names or bookmarks.
