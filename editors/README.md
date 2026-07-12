# Editor support

## VS Code

`vscode/` is a self-contained VS Code extension for Tantu: syntax highlighting
(TextMate grammar), a file icon for `.tn` files (amber crosshair, theme-adaptive),
and editing basics (`#` comment toggling, bracket/quote auto-closing, indent after `:`).

Install it locally by symlinking it into your extensions folder and reloading VS Code:

```sh
ln -s "$(pwd)/editors/vscode" ~/.vscode/extensions/tantu-lang.tantu-0.1.0
```

Because it's a symlink, edits to the grammar or icons apply on the next window reload
(`Developer: Reload Window`).

To share it as an installable package instead, build a `.vsix`:

```sh
cd editors/vscode && npx @vscode/vsce package
```

The file icon renders under icon themes that show language icons — the default Seti
theme does. Third-party icon themes (e.g. Material Icon Theme) use their own catalogs
and may need a per-theme file association for `.tn`.

## Other editors

`vscode/syntaxes/tantu.tmLanguage.json` is a standard TextMate grammar — for Sublime
Text or any TextMate-compatible editor, install it as a `.tmLanguage` grammar for the
`.tn` extension.
