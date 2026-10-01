# Configuration files

keepwatch reads two kinds of TOML file:

- the **global config**, `$XDG_CONFIG_HOME/keepwatch/config.toml` (or `--config PATH` / `KEEPWATCH_CONFIG`), which is optional; `keepwatch init` writes one with every key commented out;
- each watch's **config.toml**, which is required (it may be empty).

General rules:

- **Durations** are a number of seconds or a string of integer+unit pairs with the units `s`, `m`, `h`, `d`: `"30s"`, `"15m"`, `"1h30m"`, `"90d"`. `nan` and infinity are rejected.
- **Unknown keys are errors** everywhere except inside `[settings]` and `[environment]`, so a typo cannot silently do nothing. Errors give the file, the line, the problem and the nearest valid key.
- `~` and `$VARIABLES` are expanded in `watch_dirs` (relative entries are relative to the config file's directory); `[settings]` is passed to hooks as written, except that TOML dates and times become ISO 8601 strings.
- Changes apply while the service runs; see `keepwatch docs reload`.
- `keepwatch validate` checks every file and reports every problem at once.- **Windows paths** in TOML: `\` starts an escape inside double quotes, so write paths in single-quoted literal strings (`'C:\Users\me\in'`) or with forward slashes (`"C:/Users/me/in"`).
