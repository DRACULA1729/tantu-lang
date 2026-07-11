# Editor support

`tantu.tmLanguage.json` is a TextMate grammar for `.tn` files — comments, strings, numbers,
keywords, types, and function calls.

For VS Code, put it in a minimal local extension (a folder under `~/.vscode/extensions/` with
a `package.json` that declares a `tantu` language for the `.tn` extension and points
`grammars` at this file). For Sublime Text or a TextMate-compatible editor, install it as a
`.tmLanguage` grammar for the `.tn` extension.
